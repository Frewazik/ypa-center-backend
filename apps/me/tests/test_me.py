from __future__ import annotations

import datetime

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.events.services import RegistrationSubmission, register_for_event
from apps.events.tests.factories import EventFactory
from apps.schedule.models import MaskType
from apps.schedule.tests.factories import (
    ActivityFactory,
    EnrollmentFactory,
    ParentFactory,
    ScheduleFactory,
    ScheduleMaskFactory,
    StudentFactory,
    SubscriptionFactory,
)
from apps.users.models import Parent, Student
from apps.billing.models import SubscriptionStatus

pytestmark = pytest.mark.django_db

PROFILE_URL = "/api/v1/me/profile/"
CHILDREN_URL = "/api/v1/me/children/"
SUBSCRIPTIONS_URL = "/api/v1/me/subscriptions/"
UPCOMING_URL = "/api/v1/me/upcoming/"


@pytest.fixture
def parent() -> Parent:
    return ParentFactory(full_name="Иванова Ольга Евгеньевна", phone="+79999999999")


@pytest.fixture
def api_client(parent: Parent) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=parent)
    return client


class TestProfile:
    def test_requires_auth(self) -> None:
        response = APIClient().get(PROFILE_URL)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_returns_profile_with_children(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        StudentFactory(parent=parent, full_name="Иванов Иван")
        StudentFactory(parent=parent, full_name="Иванова Марья")
        StudentFactory()

        response = api_client.get(PROFILE_URL)

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["full_name"] == "Иванова Ольга Евгеньевна"
        assert len(payload["children"]) == 2

    def test_patch_updates_contact_fields(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        response = api_client.patch(
            PROFILE_URL, {"full_name": "Ольга Петрова"}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        parent.refresh_from_db()
        assert parent.full_name == "Ольга Петрова"

    def test_email_is_read_only(self, api_client: APIClient, parent: Parent) -> None:
        original = parent.email

        api_client.patch(PROFILE_URL, {"email": "hacker@example.com"}, format="json")

        parent.refresh_from_db()
        assert parent.email == original


class TestChildren:
    def test_creates_child(self, api_client: APIClient, parent: Parent) -> None:
        response = api_client.post(
            CHILDREN_URL,
            {"full_name": "Иванов Иван", "dob": "2015-03-12", "school_grade": "5"},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        child = Student.objects.get(parent=parent)
        assert child.full_name == "Иванов Иван"

    def test_twins_with_different_names_allowed(self, api_client: APIClient) -> None:
        first = api_client.post(
            CHILDREN_URL,
            {"full_name": "Иванов Иван", "dob": "2017-06-11"},
            format="json",
        )
        twin = api_client.post(
            CHILDREN_URL,
            {"full_name": "Иванов Пётр", "dob": "2017-06-11"},
            format="json",
        )

        assert first.status_code == status.HTTP_201_CREATED
        assert twin.status_code == status.HTTP_201_CREATED

    def test_duplicate_child_rejected(self, api_client: APIClient) -> None:
        payload = {"full_name": "Иванов Иван", "dob": "2015-03-12"}
        api_client.post(CHILDREN_URL, payload, format="json")

        duplicate = api_client.post(CHILDREN_URL, payload, format="json")

        assert duplicate.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert Student.objects.count() == 1

    def test_cannot_update_foreign_child(self, api_client: APIClient) -> None:
        foreign_child = StudentFactory()

        response = api_client.patch(
            f"{CHILDREN_URL}{foreign_child.pk}/",
            {"full_name": "Взломан"},
            format="json",
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestSubscriptions:
    def test_lists_subscriptions_with_slots(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        student = StudentFactory(parent=parent, full_name="Иванов Иван")
        subscription = SubscriptionFactory(
            parent=parent, status=SubscriptionStatus.ACTIVE
        )
        schedule = ScheduleFactory(activity=ActivityFactory(name="Шахматы"))
        EnrollmentFactory(student=student, subscription=subscription, schedule=schedule)
        SubscriptionFactory()

        response = api_client.get(SUBSCRIPTIONS_URL)

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]["display_id"] == f"#SUB-{subscription.pk}"
        assert payload[0]["student_name"] == "Иванов Иван"
        assert payload[0]["slots"][0]["activity_name"] == "Шахматы"


class TestUpcomingFeed:
    def test_projects_enrollment_to_dates(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        student = StudentFactory(parent=parent)
        schedule = ScheduleFactory(activity=ActivityFactory(name="Шахматы"))
        EnrollmentFactory(student=student, schedule=schedule)

        response = api_client.get(UPCOMING_URL, {"weeks": 2})

        assert response.status_code == status.HTTP_200_OK
        sessions = [
            item for item in response.json() if item["kind"] == "SUBSCRIPTION_SESSION"
        ]
        assert len(sessions) == 2
        assert sessions[0]["activity_name"] == "Шахматы"
        assert all(
            datetime.date.fromisoformat(item["date"]).weekday() == schedule.day_of_week
            for item in sessions
        )

    def test_cancellation_mask_hides_session(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        student = StudentFactory(parent=parent)
        schedule = ScheduleFactory()
        EnrollmentFactory(student=student, schedule=schedule)
        today = timezone.localdate()
        days_ahead = (schedule.day_of_week - today.weekday()) % 7
        first_session = today + datetime.timedelta(days=days_ahead)
        ScheduleMaskFactory(
            schedule=schedule, target_date=first_session, type=MaskType.CANCELLATION
        )

        response = api_client.get(UPCOMING_URL, {"weeks": 1})

        dates = {item["date"] for item in response.json()}
        assert first_session.isoformat() not in dates

    def test_event_matched_by_phone(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        event = EventFactory(
            title="Настольные игры",
            start_datetime=timezone.now() + datetime.timedelta(days=3),
        )
        register_for_event(
            event.pk,
            RegistrationSubmission(
                child_name="Иван",
                parent_name="Ольга",
                phone=str(parent.phone),
                email="",
                attendees_count=2,
                source="",
                comment="",
            ),
        )

        response = api_client.get(UPCOMING_URL)

        events = [item for item in response.json() if item["kind"] == "EVENT"]
        assert len(events) == 1
        assert events[0]["title"] == "Настольные игры"

    def test_child_filter(self, api_client: APIClient, parent: Parent) -> None:
        first = StudentFactory(parent=parent)
        second = StudentFactory(parent=parent)
        EnrollmentFactory(student=first, schedule=ScheduleFactory())
        EnrollmentFactory(student=second, schedule=ScheduleFactory())

        response = api_client.get(UPCOMING_URL, {"child_id": first.pk, "weeks": 1})

        student_ids = {item["student_id"] for item in response.json()}
        assert student_ids == {first.pk}
