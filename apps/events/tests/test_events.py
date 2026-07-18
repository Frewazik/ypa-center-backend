from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator

import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.test import APIClient

from apps.events.admin import EventRegistrationAdmin
from apps.events.models import EventRegistration, RegistrationStatus
from apps.events.services import (
    PENDING_PAYMENT_TTL,
    RegistrationSubmission,
    cancel_registration,
    register_for_event,
    release_expired_pending_registrations,
)
from apps.events.tests.factories import EventFactory, EventRegistrationFactory
from apps.schedule.tests.factories import ParentFactory

pytestmark = pytest.mark.django_db

THROTTLE_LIMIT = 3


def _register_url(event_id: int) -> str:
    return f"/api/v1/public/events/{event_id}/register/"


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


@pytest.fixture(autouse=True)
def _isolated_cache() -> Iterator[None]:
    # !!!: изолированный LocMemCache гарантирует, что счетчики троттлинга
    # и кэш не протекут между независимыми тестами и параллельными xdist-воркерами
    with override_settings(
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": f"events-{uuid.uuid4()}",
            }
        }
    ):
        yield


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def registration_payload() -> dict[str, object]:
    return {
        "child_name": "Миша",
        "parent_name": "Ольга",
        "phone": "+79991234567",
        "email": "olga@example.com",
        "attendees_count": 2,
        "source": "instagram",
        "comment": "Будем вдвоём с младшей сестрой",
    }


class TestRegisterForEventService:
    def test_free_event_confirms_and_takes_seats(self) -> None:
        event = EventFactory(price=0)

        registration = register_for_event(event.pk, _submission(attendees_count=2))

        event.refresh_from_db()
        assert registration.status == RegistrationStatus.CONFIRMED
        assert event.seats_taken == 2

    def test_paid_event_waits_for_payment(self) -> None:
        event = EventFactory(paid=True)

        registration = register_for_event(event.pk, _submission())

        assert registration.status == RegistrationStatus.PENDING_PAYMENT

    def test_rejects_when_not_enough_seats(self) -> None:
        event = EventFactory(capacity=5)
        EventRegistrationFactory(
            event=event, attendees_count=4, status=RegistrationStatus.CONFIRMED
        )

        with pytest.raises(ValidationError):
            register_for_event(event.pk, _submission(attendees_count=2))

        event.refresh_from_db()
        assert event.seats_taken == 4

    def test_pending_payment_blocks_seats(self) -> None:
        event = EventFactory(capacity=3)
        EventRegistrationFactory(
            event=event, attendees_count=3, status=RegistrationStatus.PENDING_PAYMENT
        )

        with pytest.raises(ValidationError):
            register_for_event(event.pk, _submission())

    def test_rejects_past_event(self) -> None:
        event = EventFactory(past=True)

        with pytest.raises(ValidationError):
            register_for_event(event.pk, _submission())

    def test_unpublished_event_is_not_found(self) -> None:
        event = EventFactory(is_published=False)

        with pytest.raises(NotFound):
            register_for_event(event.pk, _submission())


class TestCancelRegistration:
    def test_cancel_releases_seats(self) -> None:
        event = EventFactory()
        registration = register_for_event(event.pk, _submission(attendees_count=2))

        assert cancel_registration(registration.pk) is True

        event.refresh_from_db()
        registration.refresh_from_db()
        assert registration.status == RegistrationStatus.CANCELED
        assert event.seats_taken == 0

    def test_cancel_is_idempotent(self) -> None:
        event = EventFactory()
        registration = register_for_event(event.pk, _submission(attendees_count=2))

        assert cancel_registration(registration.pk) is True
        assert cancel_registration(registration.pk) is False

        event.refresh_from_db()
        assert event.seats_taken == 0

    def test_release_expired_pending_registrations(self) -> None:
        event = EventFactory(paid=True)
        expired = register_for_event(event.pk, _submission(attendees_count=2))
        EventRegistration.objects.filter(pk=expired.pk).update(
            created_at=timezone.now()
            - PENDING_PAYMENT_TTL
            - datetime.timedelta(minutes=1)
        )
        fresh = register_for_event(
            event.pk, _submission(attendees_count=1, phone="+79991234568")
        )

        released = release_expired_pending_registrations()

        event.refresh_from_db()
        expired.refresh_from_db()
        fresh.refresh_from_db()
        assert released == 1
        assert expired.status == RegistrationStatus.CANCELED
        assert fresh.status == RegistrationStatus.PENDING_PAYMENT
        assert event.seats_taken == 1


class TestEventRegistrationEndpoint:
    def test_creates_registration(
        self, api_client: APIClient, registration_payload: dict[str, object]
    ) -> None:
        event = EventFactory(price=0)

        response = api_client.post(
            _register_url(event.pk), registration_payload, format="json"
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json() == {"status": "accepted"}
        registration = EventRegistration.objects.get(event=event)
        assert registration.attendees_count == 2
        assert registration.status == RegistrationStatus.CONFIRMED

    def test_honeypot_drops_silently(
        self, api_client: APIClient, registration_payload: dict[str, object]
    ) -> None:
        # ПОЧЕМУ: скрытая ловушка (honeypot) для защиты от спам-ботов
        # при заполнении фейкового поля API возвращает успех, но тихо отбрасывает данные
        event = EventFactory()
        registration_payload["website_url"] = "https://spam.example.com"

        response = api_client.post(
            _register_url(event.pk), registration_payload, format="json"
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json() == {"status": "accepted"}
        assert not EventRegistration.objects.exists()

    def test_overbooking_returns_422(
        self, api_client: APIClient, registration_payload: dict[str, object]
    ) -> None:
        event = EventFactory(capacity=1)

        response = api_client.post(
            _register_url(event.pk), registration_payload, format="json"
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert not EventRegistration.objects.exists()

    def test_missing_event_returns_404(
        self, api_client: APIClient, registration_payload: dict[str, object]
    ) -> None:
        response = api_client.post(
            _register_url(999_999), registration_payload, format="json"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_invalid_phone_returns_422(
        self, api_client: APIClient, registration_payload: dict[str, object]
    ) -> None:
        event = EventFactory()
        registration_payload["phone"] = "not-a-phone"

        response = api_client.post(
            _register_url(event.pk), registration_payload, format="json"
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_throttles_by_ip(
        self, api_client: APIClient, registration_payload: dict[str, object]
    ) -> None:
        event = EventFactory(capacity=100)

        for _ in range(THROTTLE_LIMIT):
            ok = api_client.post(
                _register_url(event.pk), registration_payload, format="json"
            )
            assert ok.status_code == status.HTTP_201_CREATED

        throttled = api_client.post(
            _register_url(event.pk), registration_payload, format="json"
        )

        assert throttled.status_code == status.HTTP_429_TOO_MANY_REQUESTS


class TestEventRegistrationAdminGuards:
    def test_add_and_delete_disabled(self) -> None:
        # !!!: регресс-тест на защиту инварианта seats_taken
        # создание или удаление записей через админку в обход доменных сервисов
        # неизбежно приведет к рассинхронизации счетчика занятых мест
        registration_admin = EventRegistrationAdmin(EventRegistration, AdminSite())
        request = RequestFactory().get("/admin/events/eventregistration/")

        assert registration_admin.has_add_permission(request) is False
        assert registration_admin.has_delete_permission(request) is False

    def test_seat_affecting_fields_are_readonly(self) -> None:
        # ПОЧЕМУ: изменение статуса или количества гостей через админку сломает
        # расчет свободных мест, такие мутации разрешены строго через сервисы домена
        registration_admin = EventRegistrationAdmin(EventRegistration, AdminSite())

        assert {"event", "attendees_count", "status"} <= set(
            registration_admin.readonly_fields
        )


class TestRegistrationParentBinding:
    def test_authenticated_registration_binds_parent(
        self, api_client: APIClient, registration_payload: dict[str, object]
    ) -> None:
        parent = ParentFactory()
        api_client.force_authenticate(user=parent)
        event = EventFactory()

        response = api_client.post(
            _register_url(event.pk), registration_payload, format="json"
        )

        assert response.status_code == status.HTTP_201_CREATED
        registration = EventRegistration.objects.get(event=event)
        assert registration.parent_id == parent.pk

    def test_anonymous_registration_has_no_parent(
        self, api_client: APIClient, registration_payload: dict[str, object]
    ) -> None:
        event = EventFactory()

        api_client.post(_register_url(event.pk), registration_payload, format="json")

        assert EventRegistration.objects.get(event=event).parent_id is None
