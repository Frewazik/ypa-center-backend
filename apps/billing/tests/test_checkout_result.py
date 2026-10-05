# Страница «результат оплаты»: исход для родителя по транзакции и досрочная
# сверка зависшего платежа. Исходы получаем настоящими путями (чекаут, вебхук,
# свипер, процессор возвратов), подменяется только граница ЮКассы

from __future__ import annotations

import time
import uuid
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from apps.billing.models import RefundStatus, Transaction, TransactionStatus
from apps.billing.services import (
    BillingError,
    _refund_reason,
    confirm_payment,
    get_checkout_outcome,
    issue_pending_refunds,
)
from apps.billing.tests.test_billing import (
    FakeGateway,
    FakeSchedulePort,
    ParentFactory,
    _gateway_for,
    _make_pending_payment,
    _port,
    _sweep_unpaid,
)
from apps.billing.tests.test_trials import _trial_checkout
from apps.billing.views import CheckoutTransactionView
from apps.users.models import Parent
from apps.events.ports import DjangoEventBookingPort


def _get(user: Parent, transaction_id: object):  # noqa: ANN202 — DRF Response
    request = APIRequestFactory().get(f"/api/v1/checkout/transactions/{transaction_id}")
    force_authenticate(request, user=user)
    response = CheckoutTransactionView.as_view()(
        request, transaction_id=str(transaction_id)
    )
    response.render()
    return response


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    # ПОЧЕМУ: в тестах брокер InMemory и выполняет задачу прямо в kiq — без
    # подмены «постановка» сходила бы в ЮКассу. Записываем, что поставили
    calls: list[str] = []

    def fake_kiq_safely(task: object, *args: object) -> None:
        assert getattr(task, "task_name", "").endswith("verify_and_process_payment")
        calls.append(str(args[0]))

    monkeypatch.setattr("apps.billing.services.kiq_safely", fake_kiq_safely)
    return calls


@pytest.fixture(autouse=True)
def _no_yookassa_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("ручка статуса не должна ходить в ЮКассу")

    monkeypatch.setattr("apps.billing.adapters.YookassaHttpGateway._request", forbidden)


@pytest.mark.django_db
class TestOutcomes:
    def test_pending_subscription(self, enqueued: list[str]) -> None:
        tx = _make_pending_payment([101, 102])

        response = _get(tx.parent, tx.pk)

        assert response.status_code == status.HTTP_200_OK, response.data
        body = response.data
        assert body["id"] == str(tx.pk)
        assert body["type"] == "SUBSCRIPTION"
        assert body["status"] == "PENDING"
        assert body["reason"] is None
        assert body["amount"] == tx.amount
        expires = tx.created_at + timedelta(minutes=15)
        assert (
            body["expires_at"]
            == expires.astimezone(timezone.get_current_timezone()).isoformat()
        )
        order = body["order"]
        assert order["title"].startswith("Абонемент «")
        assert order["student_name"]
        assert order["trial_date"] is None
        assert sorted(slot["schedule_id"] for slot in order["slots"]) == [101, 102]
        assert set(order["slots"][0]) == {
            "schedule_id",
            "activity_name",
            "group_name",
            "day_of_week",
            "start_time",
            "end_time",
        }

    def test_succeeded(self) -> None:
        tx = _make_pending_payment([101])
        payment_id, gateway = _gateway_for(tx, "succeeded")
        confirm_payment(
            payment_id=payment_id,
            gateway=gateway,
            schedule_port=FakeSchedulePort(),
            event_port=DjangoEventBookingPort(),
        )

        body = _get(tx.parent, tx.pk).data

        assert body["status"] == "SUCCEEDED"
        assert body["reason"] is None

    def test_canceled_by_bank(self) -> None:
        tx = _make_pending_payment([101])
        payment_id, gateway = _gateway_for(tx, "canceled")
        confirm_payment(
            payment_id=payment_id,
            gateway=gateway,
            schedule_port=FakeSchedulePort(),
            event_port=DjangoEventBookingPort(),
        )

        body = _get(tx.parent, tx.pk).data

        assert body["status"] == "CANCELED"
        assert body["reason"] is None

    def test_canceled_by_ttl(self) -> None:
        tx = _make_pending_payment([101])
        _sweep_unpaid(now=timezone.now() + timedelta(minutes=20))

        assert _get(tx.parent, tx.pk).data["status"] == "CANCELED"

    def test_seats_taken_after_payment_is_refund_while_db_says_succeeded(
        self,
    ) -> None:
        tx = _make_pending_payment([101])
        payment_id, gateway = _gateway_for(tx, "succeeded")
        with pytest.raises(BillingError):
            confirm_payment(
                payment_id=payment_id,
                gateway=gateway,
                schedule_port=_port(s101=0),
                event_port=DjangoEventBookingPort(),
            )
        tx.refresh_from_db()
        assert tx.status == TransactionStatus.SUCCEEDED

        body = _get(tx.parent, tx.pk).data

        assert body["status"] == "REFUND"
        assert body["reason"] == "SEATS_TAKEN"

    def test_refund_stays_refund_after_it_was_sent(self) -> None:
        # ПОЧЕМУ: процессор сбрасывает requires_compensation при отправке
        # возврата — исход обязан держаться на refund_status
        tx = _make_pending_payment([101])
        payment_id, gateway = _gateway_for(tx, "succeeded")
        with pytest.raises(BillingError):
            confirm_payment(
                payment_id=payment_id,
                gateway=gateway,
                schedule_port=_port(s101=0),
                event_port=DjangoEventBookingPort(),
            )
        issue_pending_refunds(gateway=FakeGateway())
        tx.refresh_from_db()
        assert tx.requires_compensation is False
        assert tx.refund_status == RefundStatus.SUCCEEDED

        body = _get(tx.parent, tx.pk).data

        assert body["status"] == "REFUND"
        assert body["reason"] == "SEATS_TAKEN"

    def test_paid_after_expiry_is_refund_while_db_says_canceled(self) -> None:
        tx = _make_pending_payment([101])
        _sweep_unpaid(now=timezone.now() + timedelta(minutes=20))
        payment_id, gateway = _gateway_for(tx, "succeeded")
        with pytest.raises(BillingError):
            confirm_payment(
                payment_id=payment_id,
                gateway=gateway,
                schedule_port=FakeSchedulePort(),
                event_port=DjangoEventBookingPort(),
            )
        tx.refresh_from_db()
        assert tx.status == TransactionStatus.CANCELED

        body = _get(tx.parent, tx.pk).data

        assert body["status"] == "REFUND"
        assert body["reason"] == "PAID_AFTER_EXPIRY"

    def test_amount_mismatch_shows_received_amount(self) -> None:
        tx = _make_pending_payment([101])
        payment_id, gateway = _gateway_for(tx, "succeeded", amount_kopecks=100)
        with pytest.raises(BillingError):
            confirm_payment(
                payment_id=payment_id,
                gateway=gateway,
                schedule_port=FakeSchedulePort(),
                event_port=DjangoEventBookingPort(),
            )

        body = _get(tx.parent, tx.pk).data

        assert body["status"] == "REFUND"
        assert body["reason"] == "AMOUNT_MISMATCH"
        assert body["amount"] == 100

    def test_trial_order(self) -> None:
        result, parent, student = _trial_checkout(101)

        body = _get(parent, result.transaction_id).data

        assert body["type"] == "TRIAL"
        assert body["order"]["title"] == "Пробное занятие"
        assert body["order"]["student_name"] == student.full_name
        assert body["order"]["trial_date"] is not None
        assert [s["schedule_id"] for s in body["order"]["slots"]] == [101]

    @pytest.mark.parametrize(
        ("metadata", "expected"),
        [
            ({"reason": "HOLD_LOST"}, "PAID_AFTER_EXPIRY"),
            ({"reason": "SLOT_REMOVED"}, "GROUP_CLOSED"),
            ({"reason": "SUBSCRIPTION_NOT_ACTIVATABLE"}, "NOT_FULFILLED"),
            ({"failure_reason": "DATA_INTEGRITY"}, "NOT_FULFILLED"),
            ({}, "NOT_FULFILLED"),
        ],
    )
    def test_refund_reason_mapping(
        self, metadata: dict[str, object], expected: str
    ) -> None:
        assert _refund_reason(Transaction(metadata=metadata)) == expected


@pytest.mark.django_db
class TestAccess:
    def test_foreign_transaction_is_404(self) -> None:
        tx = _make_pending_payment([101])

        response = _get(ParentFactory(), tx.pk)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_missing_and_malformed_ids_are_404(self) -> None:
        parent = ParentFactory()

        assert _get(parent, uuid.uuid4()).status_code == status.HTTP_404_NOT_FOUND
        assert _get(parent, "not-a-uuid").status_code == status.HTTP_404_NOT_FOUND

    def test_without_token_is_401(self) -> None:
        tx = _make_pending_payment([101])

        response = APIClient().get(f"/api/v1/checkout/transactions/{tx.pk}")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.fixture
def poll(django_capture_on_commit_callbacks):  # noqa: ANN001, ANN201
    # ПОЧЕМУ свой блок на каждый опрос: задача ставится в on_commit, а колбэки
    # выполняются на выходе из блока — как в проде по концу запроса
    start = timezone.now()

    def _poll(tx: Transaction, seconds_after_start: float) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            get_checkout_outcome(
                tx.pk,
                tx.parent_id,
                now=start + timedelta(seconds=seconds_after_start),
            )

    return _poll


@pytest.mark.django_db
class TestPaymentRecheck:
    def test_enqueued_after_grace_and_not_more_often_than_interval(
        self, enqueued: list[str], poll
    ) -> None:  # noqa: ANN001
        tx = _make_pending_payment([101])

        # Опрос раз в 2 секунды: первые 10 секунд ждём вебхук
        for second in range(0, 10, 2):
            poll(tx, second)
        assert enqueued == []

        poll(tx, 10)
        assert enqueued == [tx.external_id]

        # До конца интервала — ни одной новой задачи
        for second in range(12, 24, 2):
            poll(tx, second)
        assert enqueued == [tx.external_id]

        # Интервал истёк (ключ в Redis протух) — следующая проверка
        cache.delete(f"billing:payment-recheck:lock:{tx.pk}")
        poll(tx, 26)
        assert enqueued == [tx.external_id, tx.external_id]

    def test_not_enqueued_for_final_or_unregistered_payment(
        self, enqueued: list[str], poll
    ) -> None:  # noqa: ANN001
        paid = _make_pending_payment([101])
        payment_id, gateway = _gateway_for(paid, "succeeded")
        confirm_payment(
            payment_id=payment_id,
            gateway=gateway,
            schedule_port=FakeSchedulePort(),
            event_port=DjangoEventBookingPort(),
        )
        unregistered = _make_pending_payment([102])
        Transaction.objects.filter(pk=unregistered.pk).update(external_id=None)

        for tx in (paid, unregistered):
            poll(tx, 0)
            poll(tx, 60)

        assert enqueued == []

    def test_http_poll_only_enqueues(
        self, enqueued: list[str], django_capture_on_commit_callbacks
    ) -> None:  # noqa: ANN001
        # ПОЧЕМУ: _no_yookassa_network роняет любой HTTP к ЮКассе — ручка
        # отвечает PENDING и лишь ставит задачу
        tx = _make_pending_payment([101])
        cache.set(f"billing:payment-recheck:seen:{tx.pk}", time.time() - 60)

        with django_capture_on_commit_callbacks(execute=True):
            response = _get(tx.parent, tx.pk)

        assert response.data["status"] == "PENDING"
        assert enqueued == [tx.external_id]

    def test_cache_failure_does_not_break_status(
        self, enqueued: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tx = _make_pending_payment([101])

        def broken(*args: object, **kwargs: object) -> None:
            raise ConnectionError("redis down")

        monkeypatch.setattr("apps.billing.services.cache.get_or_set", broken)

        response = _get(tx.parent, tx.pk)

        assert response.status_code == status.HTTP_200_OK
        assert enqueued == []
