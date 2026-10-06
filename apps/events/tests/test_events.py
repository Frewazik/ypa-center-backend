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

from apps.billing.ports import EventPriceChangedError
from apps.events.admin import EventAdmin, EventRegistrationAdmin
from apps.events.models import Event, EventRegistration, RegistrationStatus
from apps.events.services import (
    pending_payment_ttl,
    RegistrationSubmission,
    cancel_registration,
    register_for_event,
    release_expired_pending_registrations,
)
from apps.events.tests.factories import EventFactory, EventRegistrationFactory
from apps.schedule.tests.factories import ParentFactory
from apps.users.models import ConsentPurpose, PersonalDataConsent

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
        "pd_consent": True,
    }


class TestRegisterForEventService:
    def test_free_event_confirms_and_takes_seats(self) -> None:
        event = EventFactory(price=0)

        registration = register_for_event(event.pk, _submission(attendees_count=2))

        event.refresh_from_db()
        assert registration.status == RegistrationStatus.CONFIRMED
        assert event.seats_taken == 2

    def test_paid_event_is_not_booked_without_online_payment(self) -> None:
        # ПОЧЕМУ: платная бронь создаётся только вместе с платежом
        # (billing.create_event_payment) — «оплата на месте» больше не заводится
        event = EventFactory(paid=True)

        with pytest.raises(EventPriceChangedError):
            register_for_event(event.pk, _submission())

        event.refresh_from_db()
        assert event.seats_taken == 0
        assert not EventRegistration.objects.exists()

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
        # ПОЧЕМУ фабрика: бронь «оплата на месте» (без транзакции) новым кодом
        # не создаётся, но живые на момент выкладки свипер events снимает
        event = EventFactory(paid=True)
        expired = EventRegistrationFactory(
            event=event, attendees_count=2, status=RegistrationStatus.PENDING_PAYMENT
        )
        EventRegistration.objects.filter(pk=expired.pk).update(
            created_at=timezone.now()
            - pending_payment_ttl()
            - datetime.timedelta(minutes=1)
        )
        fresh = EventRegistrationFactory(
            event=event, attendees_count=1, status=RegistrationStatus.PENDING_PAYMENT
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

        # ПОЧЕМУ: номер на каждую заявку свой — повтор номера на то же событие
        # отклоняется раньше лимита (422)
        for i in range(THROTTLE_LIMIT):
            ok = api_client.post(
                _register_url(event.pk),
                {**registration_payload, "phone": f"+7999123450{i}"},
                format="json",
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


class TestEventAdminSave:
    def test_edit_keeps_seats_booked_after_form_was_loaded(self) -> None:
        # !!!: регресс-тест lost update: полный save() из формы записывал
        # seats_taken, прочитанный до параллельной брони, — места продавались дважды
        event = EventFactory(capacity=20)
        event_admin = EventAdmin(Event, AdminSite())
        request = RequestFactory().post("/admin/events/event/")
        loaded_by_admin = Event.objects.get(pk=event.pk)

        register_for_event(event.pk, _submission(attendees_count=2))
        loaded_by_admin.title = "Новое название"
        form = event_admin.get_form(request, loaded_by_admin)(instance=loaded_by_admin)
        event_admin.save_model(request, loaded_by_admin, form, change=True)

        event.refresh_from_db()
        assert event.title == "Новое название"
        assert event.seats_taken == 2

    def test_seats_taken_is_readonly(self) -> None:
        event_admin = EventAdmin(Event, AdminSite())

        assert "seats_taken" in event_admin.readonly_fields


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


class TestEventRegistrationConsent:
    @pytest.mark.parametrize("consent", [None, False])
    def test_registration_without_consent_rejected_and_seats_kept(
        self,
        api_client: APIClient,
        registration_payload: dict[str, object],
        consent: bool | None,
    ) -> None:
        event = EventFactory(price=0)
        payload = dict(registration_payload)
        if consent is None:
            payload.pop("pd_consent")
        else:
            payload["pd_consent"] = consent

        response = api_client.post(_register_url(event.pk), payload, format="json")

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        event.refresh_from_db()
        assert event.seats_taken == 0
        assert PersonalDataConsent.objects.count() == 0

    def test_guest_registration_journals_consent(
        self, api_client: APIClient, registration_payload: dict[str, object]
    ) -> None:
        event = EventFactory(price=0)

        api_client.post(_register_url(event.pk), registration_payload, format="json")

        record = PersonalDataConsent.objects.get()
        assert record.purpose == ConsentPurpose.EVENT_REGISTRATION
        assert record.source_id == EventRegistration.objects.get().pk
        assert record.parent is None
        assert record.email == registration_payload["email"]

    def test_logged_in_parent_linked_to_consent(
        self, registration_payload: dict[str, object]
    ) -> None:
        parent = ParentFactory()
        client = APIClient()
        client.force_authenticate(user=parent)
        event = EventFactory(price=0)

        client.post(_register_url(event.pk), registration_payload, format="json")

        assert PersonalDataConsent.objects.get().parent == parent

    def test_staff_created_registration_has_no_site_consent(self) -> None:
        # ПОЧЕМУ: регистрацию по звонку заводит сотрудник — согласие на сайте
        # не давалось, выдумывать запись журнала нельзя
        event = EventFactory(price=0)

        register_for_event(
            event.pk,
            RegistrationSubmission(
                child_name="Миша",
                parent_name="Ольга",
                phone="+79991234567",
                email="",
                attendees_count=1,
                source="",
                comment="",
            ),
        )

        assert PersonalDataConsent.objects.count() == 0
