from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib import messages
from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core import mail
from django.http import HttpRequest
from django.test import RequestFactory, override_settings
from django.utils import timezone
from pytest_django.fixtures import DjangoCaptureOnCommitCallbacks
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from apps.events import services
from apps.events.admin import EventRegistrationAdmin
from apps.events.models import Event, EventRegistration, RegistrationStatus
from apps.events.services import (
    MAX_ATTENDEES_PER_REGISTRATION,
    RegistrationSubmission,
    cancel_registration,
    register_for_event,
    release_expired_pending_registrations,
)
from apps.events.tasks import send_registration_expired_email_task
from apps.events.tests.factories import EventFactory, EventRegistrationFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _isolated_cache() -> Iterator[None]:
    # ПОЧЕМУ: счётчик лимита по IP не должен протекать из других тестов
    with override_settings(
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": f"events-pending-{uuid.uuid4()}",
            }
        }
    ):
        yield


def _submission(**overrides: object) -> RegistrationSubmission:
    defaults: dict[str, object] = {
        "child_name": "Миша",
        "parent_name": "Ольга",
        "phone": "+79991234567",
        "email": "olga@example.com",
        "attendees_count": 1,
        "source": "instagram",
        "comment": "",
    }
    return RegistrationSubmission(**{**defaults, **overrides})  # type: ignore[arg-type]


def _legacy_pending(event: Event, **overrides: object) -> EventRegistration:
    # ПОЧЕМУ фабрика: брони «оплата на месте» новым кодом не создаются, но
    # живые на момент выкладки доживают по старым правилам — их и проверяем
    defaults: dict[str, object] = {
        "event": event,
        "status": RegistrationStatus.PENDING_PAYMENT,
        "parent_name": "Ольга",
        "email": "olga@example.com",
    }
    return EventRegistrationFactory(**{**defaults, **overrides})


def _age(registration: EventRegistration, delta: datetime.timedelta) -> None:
    EventRegistration.objects.filter(pk=registration.pk).update(
        created_at=timezone.now() - delta
    )


def _admin_request() -> HttpRequest:
    request = RequestFactory().post("/admin/")
    request.session = {}  # type: ignore[assignment]
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    return request


def _admin_confirm(registration_id: int) -> HttpRequest:
    request = _admin_request()
    admin = EventRegistrationAdmin(EventRegistration, AdminSite())
    admin.confirm_selected(
        request, EventRegistration.objects.filter(pk=registration_id)
    )
    return request


@pytest.fixture
def queued() -> Iterator[dict[str, AsyncMock]]:
    # ПОЧЕМУ: подменяется постановка в брокер — проверяем, что задача
    # действительно ушла в очередь после коммита
    with (
        patch("apps.events.tasks.notify_paid_registration_task") as notify,
        patch("apps.events.tasks.send_registration_expired_email_task") as email,
    ):
        notify.kiq = AsyncMock()
        email.kiq = AsyncMock()
        yield {"notify": notify.kiq, "email": email.kiq}


class TestSweeperVsManagerRace:
    def test_sweeper_does_not_cancel_registration_confirmed_after_selection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Сценарий гонки, разложенный по шагам:
        # 1) свипер выбрал id просроченных PENDING_PAYMENT;
        # 2) менеджер в этот момент нажал «Подтвердить оплату»;
        # 3) свипер дошёл до снятия этого id.
        # Шаг 2 вклинивается обёрткой вокруг поштучного снятия свипера
        event = EventFactory(paid=True)
        registration = _legacy_pending(event, attendees_count=2)
        _age(registration, datetime.timedelta(hours=2))

        original = services.expire_pending_registration
        calls: list[int] = []

        def confirm_then_expire(registration_id: int) -> bool:
            calls.append(registration_id)
            _admin_confirm(registration_id)
            return original(registration_id)

        monkeypatch.setattr(
            services, "expire_pending_registration", confirm_then_expire
        )

        released = release_expired_pending_registrations()

        registration.refresh_from_db()
        event.refresh_from_db()
        assert calls == [registration.pk], (
            "обёртка не сработала — тест ничего не проверил"
        )
        assert released == 0
        assert registration.status == RegistrationStatus.CONFIRMED
        assert event.seats_taken == 2

    def test_confirming_already_expired_booking_warns_manager(self) -> None:
        event = EventFactory(paid=True)
        registration = _legacy_pending(event)
        _age(registration, datetime.timedelta(hours=2))
        release_expired_pending_registrations()

        request = _admin_confirm(registration.pk)

        registration.refresh_from_db()
        assert registration.status == RegistrationStatus.CANCELED
        notes = [(m.level, str(m)) for m in messages.get_messages(request)]
        assert (messages.INFO, "Подтверждено: 0.") in notes
        assert any(
            level == messages.WARNING and "Пропущено: 1" in text
            for level, text in notes
        )


class TestPendingRegistrationLifetime:
    @override_settings(EVENT_PENDING_PAYMENT_TTL_MINUTES=60)
    def test_ttl_is_configurable(self) -> None:
        event = EventFactory(paid=True)
        fresh = _legacy_pending(event)
        expired = _legacy_pending(event)
        _age(fresh, datetime.timedelta(minutes=31))
        _age(expired, datetime.timedelta(minutes=61))

        released = release_expired_pending_registrations()

        fresh.refresh_from_db()
        expired.refresh_from_db()
        event.refresh_from_db()
        assert released == 1
        assert fresh.status == RegistrationStatus.PENDING_PAYMENT
        assert expired.status == RegistrationStatus.CANCELED
        assert event.seats_taken == 1

    def test_expired_booking_emails_family(
        self,
        queued: dict[str, AsyncMock],
        django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    ) -> None:
        registration = _legacy_pending(EventFactory(paid=True))
        _age(registration, datetime.timedelta(hours=1))

        with django_capture_on_commit_callbacks(execute=True):
            release_expired_pending_registrations()

        queued["email"].assert_awaited_once_with(registration.pk)

    def test_expired_booking_without_email_sends_nothing(
        self,
        queued: dict[str, AsyncMock],
        django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    ) -> None:
        registration = _legacy_pending(EventFactory(paid=True), email="")
        _age(registration, datetime.timedelta(hours=1))

        with django_capture_on_commit_callbacks(execute=True):
            release_expired_pending_registrations()

        queued["email"].assert_not_awaited()

    def test_manual_cancel_does_not_email_family(
        self,
        queued: dict[str, AsyncMock],
        django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    ) -> None:
        # ПОЧЕМУ: при ручной отмене менеджер говорит с семьёй сам
        registration = _legacy_pending(EventFactory(paid=True))

        with django_capture_on_commit_callbacks(execute=True):
            cancel_registration(registration.pk)

        queued["email"].assert_not_awaited()

    def test_email_task_sends_letter(self) -> None:
        event = EventFactory(paid=True, title="Театральные игры")
        registration = _legacy_pending(event)
        cancel_registration(registration.pk)

        send_registration_expired_email_task.original_func(registration.pk)

        assert len(mail.outbox) == 1
        letter = mail.outbox[0]
        assert letter.to == ["olga@example.com"]
        assert "Театральные игры" in letter.body
        assert "Ольга" in letter.body


class TestLegacyBookingConfirmedByManager:
    def test_confirmed_legacy_booking_survives_sweeper(
        self,
        queued: dict[str, AsyncMock],
        django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    ) -> None:
        event = EventFactory(paid=True)
        registration = _legacy_pending(event, attendees_count=2)

        _admin_confirm(registration.pk)
        _age(registration, datetime.timedelta(days=1))
        with django_capture_on_commit_callbacks(execute=True):
            released = release_expired_pending_registrations()

        registration.refresh_from_db()
        event.refresh_from_db()
        assert released == 0
        assert registration.status == RegistrationStatus.CONFIRMED
        assert event.seats_taken == 2
        queued["email"].assert_not_awaited()


class TestFreeEventHoarding:
    def test_same_phone_cannot_book_event_twice(self) -> None:
        event = EventFactory(capacity=40)
        register_for_event(event.pk, _submission())

        with pytest.raises(ValidationError) as exc:
            register_for_event(event.pk, _submission())

        assert "phone" in exc.value.detail  # type: ignore[operator]
        event.refresh_from_db()
        assert event.seats_taken == 1

    def test_phone_can_book_again_after_cancel(self) -> None:
        event = EventFactory()
        first = register_for_event(event.pk, _submission())
        cancel_registration(first.pk)

        again = register_for_event(event.pk, _submission())

        assert again.status == RegistrationStatus.CONFIRMED

    def test_same_phone_may_book_other_event(self) -> None:
        register_for_event(EventFactory().pk, _submission())

        other = register_for_event(EventFactory().pk, _submission())

        assert other.status == RegistrationStatus.CONFIRMED

    def test_api_caps_attendees_per_request(self) -> None:
        event = EventFactory(capacity=40)
        payload = {
            "child_name": "Миша",
            "parent_name": "Ольга",
            "phone": "+79991234567",
            "attendees_count": MAX_ATTENDEES_PER_REGISTRATION + 1,
            "pd_consent": True,
        }

        response = APIClient().post(
            f"/api/v1/public/events/{event.pk}/register/", payload, format="json"
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert not EventRegistration.objects.exists()
