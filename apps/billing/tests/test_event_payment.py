from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.contrib import messages
from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core import mail
from django.db import connection
from django.http import HttpRequest
from django.test import RequestFactory, override_settings
from django.utils import timezone
from pytest_django.fixtures import DjangoCaptureOnCommitCallbacks
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.test import APIClient

from apps.billing.adapters import PaymentInfo, PaymentStatus
from apps.billing.models import (
    IdempotencyRecord,
    RefundStatus,
    Transaction,
    TransactionStatus,
)
from apps.billing.services import (
    CheckoutResult,
    _quarantine_refund,
    create_event_payment,
    issue_pending_refunds,
    sweep_stale_pending_transactions,
)
from apps.billing.tasks import (
    notify_refund_review_task,
    run_payment_verification,
    send_refund_email_task,
)
from apps.billing.tests.test_billing import (
    FakeGateway,
    FakeSchedulePort,
    ParentFactory,
    RaisingGateway,
    SubscriptionFactory,
)
from apps.events.admin import EventRegistrationAdmin
from apps.events.models import Event, EventRegistration, RegistrationStatus
from apps.events.ports import DjangoEventBookingPort
from apps.events.services import (
    RegistrationSubmission,
    hold_paid_registration,
    release_expired_pending_registrations,
)
from apps.events.tasks import notify_paid_registration_task
from apps.events.tests.factories import EventFactory, EventRegistrationFactory
from apps.users.consent import ConsentSource

pytestmark = pytest.mark.django_db

_PRICE = 150_000
_EMAIL = "olga@example.com"
_PHONE = "+79991234567"


@pytest.fixture(autouse=True)
def _isolated_cache() -> Iterator[None]:
    # ПОЧЕМУ: лимит по IP и ключи досрочной сверки не должны протекать между тестами
    with override_settings(
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": f"event-payment-{uuid.uuid4()}",
            }
        }
    ):
        yield


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> FakeGateway:
    # ПОЧЕМУ: подменяется только HTTP-граница ЮКассы, порт событий — настоящий
    fake = FakeGateway()
    monkeypatch.setattr("apps.billing.views.YookassaHttpGateway", lambda: fake)
    return fake


@pytest.fixture
def queued() -> Iterator[dict[str, AsyncMock]]:
    with (
        patch("apps.events.tasks.notify_paid_registration_task") as notify,
        patch("apps.events.tasks.send_registration_expired_email_task") as expired,
    ):
        notify.kiq = AsyncMock()
        expired.kiq = AsyncMock()
        yield {"notify": notify.kiq, "expired_email": expired.kiq}


def _payload(**overrides: object) -> dict[str, object]:
    return {
        "child_name": "Миша",
        "parent_name": "Ольга",
        "phone": _PHONE,
        "email": _EMAIL,
        "attendees_count": 2,
        "pd_consent": True,
        **overrides,
    }


def _register(
    event: Event,
    *,
    key: str | None = None,
    client: APIClient | None = None,
    **overrides: object,
) -> Response:
    headers = {"X-Idempotency-Key": key} if key is not None else {}
    return (client or APIClient()).post(
        f"/api/v1/public/events/{event.pk}/register/",
        _payload(**overrides),
        format="json",
        headers=headers,
    )


def _checkout(event: Event, **overrides: object) -> Transaction:
    response = _register(event, key=str(uuid.uuid4()), **overrides)
    assert response.status_code == status.HTTP_201_CREATED, response.content
    return Transaction.objects.get(pk=response.json()["transaction_id"])


def _webhook(
    gateway: FakeGateway,
    tx: Transaction,
    *,
    amount: int | None = None,
    payment_status: PaymentStatus = "succeeded",
) -> None:
    # ПОЧЕМУ: вебхук = «ЮКасса говорит о платеже»; воркер перезапрашивает
    # статус у шлюза — его и подставляем, логику проводим настоящую
    payment_id = f"yk-{tx.pk}"
    gateway.payments[payment_id] = PaymentInfo(
        id=payment_id,
        status=payment_status,
        transaction_id=str(tx.pk),
        amount_kopecks=tx.amount if amount is None else amount,
        currency="RUB",
    )
    run_payment_verification(
        payment_id, gateway, FakeSchedulePort(), DjangoEventBookingPort()
    )


def _admin_request(user: object) -> HttpRequest:
    request = RequestFactory().post("/admin/")
    request.user = user  # type: ignore[assignment]
    request.session = {}  # type: ignore[assignment]
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    return request


def _admin_action(action: str, registration_id: int, user: object) -> HttpRequest:
    request = _admin_request(user)
    admin = EventRegistrationAdmin(EventRegistration, AdminSite())
    getattr(admin, action)(
        request, EventRegistration.objects.filter(pk=registration_id)
    )
    return request


def _notes(request: HttpRequest) -> list[str]:
    return [str(m) for m in messages.get_messages(request)]


class TestPaidRegistrationCheckout:
    def test_paid_registration_returns_payment_link_and_holds_seats(
        self,
        gateway: FakeGateway,
        queued: dict[str, AsyncMock],
        django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    ) -> None:
        event = EventFactory(price=_PRICE, capacity=10)

        with django_capture_on_commit_callbacks(execute=True):
            response = _register(event, key=str(uuid.uuid4()))

        assert response.status_code == status.HTTP_201_CREATED
        body = response.json()
        assert set(body) == {"transaction_id", "status", "payment_url", "expires_at"}
        assert body["status"] == "PENDING_PAYMENT"
        registration = EventRegistration.objects.get()
        tx = Transaction.objects.get(pk=body["transaction_id"])
        assert registration.status == RegistrationStatus.PENDING_PAYMENT
        assert registration.amount == _PRICE * 2
        assert tx.amount == _PRICE * 2
        assert tx.parent_id is None
        assert tx.event_registration_id == registration.pk
        assert tx.external_id == f"yk-{tx.pk}"
        assert gateway.return_kinds == ["event"]
        event.refresh_from_db()
        assert event.seats_taken == 2
        # ПОЧЕМУ: неоплаченная бронь не требует действий менеджера
        queued["notify"].assert_not_awaited()

    def test_free_event_response_is_unchanged(self, gateway: FakeGateway) -> None:
        event = EventFactory(price=0)

        response = _register(event)

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json() == {"status": "accepted"}
        registration = EventRegistration.objects.get()
        assert registration.status == RegistrationStatus.CONFIRMED
        assert registration.amount == 0
        assert not Transaction.objects.exists()
        assert gateway.created_payments == []

    def test_paid_registration_requires_idempotency_key(
        self, gateway: FakeGateway
    ) -> None:
        response = _register(EventFactory(price=_PRICE))

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert not EventRegistration.objects.exists()

    def test_paid_registration_requires_email(self, gateway: FakeGateway) -> None:
        response = _register(
            EventFactory(price=_PRICE), key=str(uuid.uuid4()), email=""
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        fields = {p["name"] for p in response.json()["extensions"]["invalid_params"]}
        assert "email" in fields
        assert not EventRegistration.objects.exists()
        assert not Transaction.objects.exists()
        # ПОЧЕМУ: ошибка формы освобождает ключ — исправив email, семья
        # повторит запрос с тем же ключом
        assert not IdempotencyRecord.objects.exists()

    def test_same_key_returns_same_payment(self, gateway: FakeGateway) -> None:
        event = EventFactory(price=_PRICE)
        key = str(uuid.uuid4())

        first = _register(event, key=key)
        second = _register(event, key=key)

        assert first.status_code == second.status_code == status.HTTP_201_CREATED
        assert first.json() == second.json()
        assert Transaction.objects.count() == 1
        assert EventRegistration.objects.count() == 1
        assert len(gateway.created_payments) == 1

    def test_same_key_with_other_form_is_conflict(self, gateway: FakeGateway) -> None:
        event = EventFactory(price=_PRICE)
        key = str(uuid.uuid4())
        _register(event, key=key)

        response = _register(event, key=key, attendees_count=3)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

    def test_honeypot_on_paid_event_answers_like_checkout(
        self, gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "apps.billing.views.event_return_url",
            lambda tx: f"https://site.ru/checkout/result?tx={tx}&kind=event",
        )

        response = _register(
            EventFactory(price=_PRICE),
            key=str(uuid.uuid4()),
            website_url="http://spam",
        )

        assert response.status_code == status.HTTP_201_CREATED
        body = response.json()
        assert set(body) == {"transaction_id", "status", "payment_url", "expires_at"}
        assert body["status"] == "PENDING_PAYMENT"
        assert body["payment_url"].endswith(f"tx={body['transaction_id']}&kind=event")
        assert not EventRegistration.objects.exists()
        assert not Transaction.objects.exists()
        assert gateway.created_payments == []

    def test_price_changed_between_form_and_booking_is_conflict(
        self, gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ПОЧЕМУ: фронт выбрал путь по старой цене — событие стало бесплатным,
        # пока семья заполняла форму
        monkeypatch.setattr("apps.events.views.is_paid_event", lambda _id: True)

        response = _register(EventFactory(price=0), key=str(uuid.uuid4()))

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["code"] == "EVENT_PRICE_CHANGED"
        assert not EventRegistration.objects.exists()
        assert not IdempotencyRecord.objects.exists()

    def test_gateway_failure_releases_seats_without_letter(
        self,
        monkeypatch: pytest.MonkeyPatch,
        queued: dict[str, AsyncMock],
        django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    ) -> None:
        monkeypatch.setattr("apps.billing.views.YookassaHttpGateway", RaisingGateway)
        event = EventFactory(price=_PRICE)

        with django_capture_on_commit_callbacks(execute=True):
            response = _register(event, key=str(uuid.uuid4()))

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert EventRegistration.objects.get().status == RegistrationStatus.CANCELED
        event.refresh_from_db()
        assert event.seats_taken == 0
        queued["expired_email"].assert_not_awaited()


class TestPaymentWebhook:
    def test_successful_payment_confirms_booking_and_tells_managers(
        self,
        gateway: FakeGateway,
        queued: dict[str, AsyncMock],
        django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    ) -> None:
        event = EventFactory(price=_PRICE)
        tx = _checkout(event)

        with django_capture_on_commit_callbacks(execute=True):
            _webhook(gateway, tx)

        tx.refresh_from_db()
        registration = EventRegistration.objects.get()
        assert tx.status == TransactionStatus.SUCCEEDED
        assert tx.received_amount == _PRICE * 2
        assert not tx.requires_compensation
        assert registration.status == RegistrationStatus.CONFIRMED
        event.refresh_from_db()
        assert event.seats_taken == 2
        queued["notify"].assert_awaited_once_with(registration.pk)

    def test_repeated_webhook_changes_nothing(
        self,
        gateway: FakeGateway,
        queued: dict[str, AsyncMock],
        django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    ) -> None:
        event = EventFactory(price=_PRICE)
        tx = _checkout(event)

        with django_capture_on_commit_callbacks(execute=True):
            _webhook(gateway, tx)
            _webhook(gateway, tx)

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.SUCCEEDED
        assert not tx.requires_compensation
        assert tx.refund_status is None
        assert EventRegistration.objects.get().status == RegistrationStatus.CONFIRMED
        event.refresh_from_db()
        assert event.seats_taken == 2
        assert queued["notify"].await_count == 1

    def test_amount_mismatch_refunds_and_releases_seats(
        self, gateway: FakeGateway
    ) -> None:
        event = EventFactory(price=_PRICE)
        tx = _checkout(event)

        _webhook(gateway, tx, amount=tx.amount - 100)

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.FAILED
        assert tx.requires_compensation
        assert tx.received_amount == _PRICE * 2 - 100
        assert EventRegistration.objects.get().status == RegistrationStatus.CANCELED
        event.refresh_from_db()
        assert event.seats_taken == 0

    def test_payment_after_ttl_release_is_refunded_not_resurrected(
        self,
        gateway: FakeGateway,
        queued: dict[str, AsyncMock],
        django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    ) -> None:
        event = EventFactory(price=_PRICE)
        tx = _checkout(event)
        gateway.payments[f"yk-{tx.pk}"] = PaymentInfo(
            id=f"yk-{tx.pk}",
            status="pending",
            transaction_id=str(tx.pk),
            amount_kopecks=tx.amount,
            currency="RUB",
        )

        # ПОЧЕМУ: снимает свипер billing и только после вопроса к ЮКассе
        with django_capture_on_commit_callbacks(execute=True):
            swept = sweep_stale_pending_transactions(
                gateway=gateway,
                schedule_port=FakeSchedulePort(),
                event_port=DjangoEventBookingPort(),
                now=timezone.now() + datetime.timedelta(minutes=16),
            )

        registration = EventRegistration.objects.get()
        tx.refresh_from_db()
        assert swept == 1
        assert tx.status == TransactionStatus.CANCELED
        assert tx.payment_recheck_until is not None
        assert registration.status == RegistrationStatus.CANCELED
        queued["expired_email"].assert_awaited_once_with(registration.pk)

        _webhook(gateway, tx)

        tx.refresh_from_db()
        registration.refresh_from_db()
        assert tx.requires_compensation
        assert tx.metadata["reason"] == "PAYMENT_SUCCEEDED_AFTER_EXPIRY"
        assert registration.status == RegistrationStatus.CANCELED
        event.refresh_from_db()
        assert event.seats_taken == 0

    def test_events_sweeper_leaves_online_booking_to_billing(
        self, gateway: FakeGateway
    ) -> None:
        tx = _checkout(EventFactory(price=_PRICE))
        EventRegistration.objects.update(
            created_at=timezone.now() - datetime.timedelta(hours=2)
        )

        assert release_expired_pending_registrations() == 0

        assert (
            EventRegistration.objects.get().status == RegistrationStatus.PENDING_PAYMENT
        )
        tx.refresh_from_db()
        assert tx.status == TransactionStatus.PENDING


@pytest.mark.django_db(transaction=True)
class TestConcurrentLastSeats:
    def test_two_checkouts_for_last_seats_only_one_wins(self) -> None:
        event = EventFactory(price=_PRICE, capacity=2)
        workers = 2
        barrier = Barrier(workers)
        consent = ConsentSource(ip="203.0.113.7", user_agent="pytest")

        def checkout(phone: str) -> CheckoutResult | ValidationError:
            submission = RegistrationSubmission(
                child_name="Миша",
                parent_name="Ольга",
                phone=phone,
                email=_EMAIL,
                attendees_count=2,
                source="",
                comment="",
            )
            barrier.wait()
            try:
                return create_event_payment(
                    hold=lambda: hold_paid_registration(
                        event.pk, submission, None, consent
                    ),
                    parent_id=None,
                    idempotency_key=str(uuid.uuid4()),
                    request_fingerprint=phone,
                    gateway=FakeGateway(),
                    event_port=DjangoEventBookingPort(),
                )
            except ValidationError as exc:
                return exc
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(checkout, ["+79991234567", "+79991234568"]))

        won = [r for r in results if isinstance(r, CheckoutResult)]
        lost = [r for r in results if isinstance(r, ValidationError)]
        assert len(won) == 1
        assert len(lost) == 1
        assert "attendees_count" in lost[0].detail  # type: ignore[operator]
        event.refresh_from_db()
        assert event.seats_taken == 2
        assert Transaction.objects.count() == 1
        assert EventRegistration.objects.count() == 1


class TestGuestPathAndCabinet:
    def test_guest_without_token_goes_from_form_to_confirmed_booking(
        self, gateway: FakeGateway
    ) -> None:
        event = EventFactory(price=_PRICE, title="Театральные игры")

        response = _register(event, key=str(uuid.uuid4()))
        tx = Transaction.objects.get(pk=response.json()["transaction_id"])
        _webhook(gateway, tx)
        result = APIClient().get(f"/api/v1/public/events/payments/{tx.pk}/")

        assert EventRegistration.objects.get().status == RegistrationStatus.CONFIRMED
        assert result.status_code == status.HTTP_200_OK
        assert result.json()["status"] == "SUCCEEDED"
        assert result.json()["order"]["title"] == "Театральные игры"

    def test_paid_guest_booking_shows_in_cabinet_with_paid_amount(
        self, gateway: FakeGateway
    ) -> None:
        event = EventFactory(price=_PRICE)
        tx = _checkout(event)
        _webhook(gateway, tx)
        # ПОЧЕМУ: после оплаты менеджер поднял цену — ЛК обязан показать уплаченное
        Event.objects.filter(pk=event.pk).update(price=_PRICE * 3)
        client = APIClient()
        client.force_authenticate(ParentFactory(email=_EMAIL))

        bookings = client.get("/api/v1/me/bookings/", {"kind": "EVENT"}).json()
        upcoming = client.get("/api/v1/me/upcoming/", {"weeks": 2}).json()

        assert [(b["id"], b["cost"], b["status"]) for b in bookings] == [
            (tx.event_registration_id, _PRICE * 2, "CONFIRMED")
        ]
        assert [item["kind"] for item in upcoming] == ["EVENT"]


class TestPaymentResultScreen:
    def test_event_outcome_has_no_personal_data(self, gateway: FakeGateway) -> None:
        tx = _checkout(EventFactory(price=_PRICE, title="Театральные игры"))

        response = APIClient().get(f"/api/v1/public/events/payments/{tx.pk}/")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["type"] == "EVENT"
        assert body["status"] == "PENDING"
        assert body["amount"] == _PRICE * 2
        assert set(body["order"]) == {
            "event_id",
            "title",
            "starts_at",
            "attendees_count",
        }
        assert body["order"]["attendees_count"] == 2
        text = json.dumps(body, ensure_ascii=False)
        for secret in ("Миша", "Ольга", _EMAIL, _PHONE, "9991234567"):
            assert secret not in text

    def test_foreign_subscription_id_is_not_found(self) -> None:
        subscription = SubscriptionFactory()
        tx = Transaction.objects.create(
            parent=subscription.parent, subscription=subscription, amount=700_000
        )

        response = APIClient().get(f"/api/v1/public/events/payments/{tx.pk}/")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_owner_checkout_screen_does_not_serve_event_payment(
        self, gateway: FakeGateway
    ) -> None:
        # ПОЧЕМУ: у вошедшего гостя транзакция события с его parent — но вид
        # заказа «событие» отдаёт только публичная ручка
        parent = ParentFactory(email=_EMAIL)
        client = APIClient()
        client.force_authenticate(parent)
        response = _register(
            EventFactory(price=_PRICE), key=str(uuid.uuid4()), client=client
        )
        tx = Transaction.objects.get(pk=response.json()["transaction_id"])
        assert tx.parent_id == parent.pk

        result = client.get(f"/api/v1/checkout/transactions/{tx.pk}")

        assert result.status_code == status.HTTP_404_NOT_FOUND


class TestManagerCancel:
    def test_cancel_of_paid_booking_queues_single_refund(
        self, gateway: FakeGateway, admin_user: object
    ) -> None:
        event = EventFactory(price=_PRICE)
        tx = _checkout(event)
        _webhook(gateway, tx)

        first = _admin_action("cancel_selected", tx.event_registration_id, admin_user)
        issue_pending_refunds(gateway=gateway)
        second = _admin_action("cancel_selected", tx.event_registration_id, admin_user)
        issue_pending_refunds(gateway=gateway)

        tx.refresh_from_db()
        event.refresh_from_db()
        assert EventRegistration.objects.get().status == RegistrationStatus.CANCELED
        assert event.seats_taken == 0
        assert gateway.refund_calls == [(f"yk-{tx.pk}", _PRICE * 2, f"refund-{tx.pk}")]
        assert tx.metadata["reason"] == "CANCELED_BY_CENTER"
        assert "Отменено: 1, поставлено возвратов: 1." in _notes(first)
        assert "Отменено: 0, поставлено возвратов: 0." in _notes(second)
        result = APIClient().get(f"/api/v1/public/events/payments/{tx.pk}/").json()
        assert (result["status"], result["reason"]) == ("REFUND", "CANCELED_BY_CENTER")

    def test_unpaid_online_booking_is_not_canceled_by_manager(
        self, gateway: FakeGateway, admin_user: object
    ) -> None:
        tx = _checkout(EventFactory(price=_PRICE))

        request = _admin_action("cancel_selected", tx.event_registration_id, admin_user)

        assert (
            EventRegistration.objects.get().status == RegistrationStatus.PENDING_PAYMENT
        )
        assert any("ждут онлайн-оплаты" in note for note in _notes(request))

    def test_confirm_button_skips_online_booking(
        self, gateway: FakeGateway, admin_user: object
    ) -> None:
        tx = _checkout(EventFactory(price=_PRICE))

        request = _admin_action(
            "confirm_selected", tx.event_registration_id, admin_user
        )

        assert (
            EventRegistration.objects.get().status == RegistrationStatus.PENDING_PAYMENT
        )
        assert any("оплачиваются онлайн" in note for note in _notes(request))

    def test_cancel_of_legacy_booking_still_works_without_refund(
        self, admin_user: object
    ) -> None:
        event = EventFactory(price=_PRICE)
        legacy = EventRegistrationFactory(
            event=event, status=RegistrationStatus.CONFIRMED, attendees_count=2
        )

        _admin_action("cancel_selected", legacy.pk, admin_user)

        legacy.refresh_from_db()
        event.refresh_from_db()
        assert legacy.status == RegistrationStatus.CANCELED
        assert event.seats_taken == 0
        assert not Transaction.objects.exists()


class TestGuestNotifications:
    def test_refund_letter_goes_to_booking_email(
        self, gateway: FakeGateway, admin_user: object
    ) -> None:
        event = EventFactory(price=_PRICE, title="Театральные игры")
        tx = _checkout(event)
        _webhook(gateway, tx)
        _admin_action("cancel_selected", tx.event_registration_id, admin_user)
        issue_pending_refunds(gateway=gateway)
        tx.refresh_from_db()
        assert tx.refund_status == RefundStatus.SUCCEEDED

        send_refund_email_task.original_func(str(tx.pk))

        assert len(mail.outbox) == 1
        letter = mail.outbox[0]
        assert letter.to == [_EMAIL]
        assert "Ольга" in letter.body
        assert "«Театральные игры»" in letter.body
        assert "отменена" in letter.body
        assert "3 000,00 ₽" in letter.body

    def test_manual_review_alert_uses_booking_contacts(
        self, gateway: FakeGateway, admin_user: object
    ) -> None:
        tx = _checkout(EventFactory(price=_PRICE))
        _webhook(gateway, tx)
        _admin_action("cancel_selected", tx.event_registration_id, admin_user)
        _quarantine_refund(tx.pk, "ЮКасса отвергла возврат")

        with patch(
            "apps.billing.tasks.send_manager_message", new_callable=AsyncMock
        ) as send:
            async_to_sync(notify_refund_review_task.original_func)(str(tx.pk))

        text = send.await_args.args[0]
        assert f"Бронь события #{tx.event_registration_id}" in text
        assert "Ольга" in text
        assert _EMAIL in text

    def test_paid_booking_message_has_contacts(self, gateway: FakeGateway) -> None:
        tx = _checkout(EventFactory(price=_PRICE, title="Театральные игры"))
        _webhook(gateway, tx)

        with patch(
            "apps.events.tasks.send_manager_message", new_callable=AsyncMock
        ) as send:
            async_to_sync(notify_paid_registration_task.original_func)(
                tx.event_registration_id
            )

        text = send.await_args.args[0]
        assert "Оплачена онлайн" in text
        assert "Театральные игры" in text
        assert "Мест: 2" in text
        assert f"Ольга, {_PHONE}, {_EMAIL}" in text
