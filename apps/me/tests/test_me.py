from __future__ import annotations

import datetime
import zoneinfo
from typing import TYPE_CHECKING

import pytest
from django.conf import settings
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.events.services import RegistrationSubmission, register_for_event
from apps.events.models import Event, EventRegistration, RegistrationStatus
from apps.events.tests.factories import EventFactory, EventRegistrationFactory
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
from apps.users.models import ConsentPurpose, Parent, PersonalDataConsent, Student
from apps.billing.models import (
    DepositEntry,
    DepositEntryReason,
    Enrollment,
    EnrollmentStatus,
    ParentDeposit,
    Subscription,
    SubscriptionStatus,
    Transaction,
    TransactionStatus,
)
from apps.billing.services import sweep_expired_subscriptions
from apps.billing.tests.factories import SubscriptionSlotFactory

if TYPE_CHECKING:
    from pytest_django import DjangoAssertNumQueries

pytestmark = pytest.mark.django_db

PROFILE_URL = "/api/v1/me/profile/"
CHILDREN_URL = "/api/v1/me/children/"
SUBSCRIPTIONS_URL = "/api/v1/me/subscriptions/"
UPCOMING_URL = "/api/v1/me/upcoming/"
TRIALS_URL = "/api/v1/me/trials/"
DEPOSIT_URL = "/api/v1/me/deposit/"
DEPOSIT_ENTRIES_URL = "/api/v1/me/deposit/entries/"
BOOKINGS_URL = "/api/v1/me/bookings/"


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

    def test_rename_into_sibling_rejected_not_500(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        # ПОЧЕМУ: раньше IntegrityError уходил клиенту 500-й
        StudentFactory(parent=parent, full_name="Иванов Иван", dob=_DOB)
        other = StudentFactory(parent=parent, full_name="Иванов Пётр", dob=_DOB)

        response = api_client.patch(
            f"{CHILDREN_URL}{other.pk}/", {"full_name": "Иванов Иван"}, format="json"
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert response.json()["code"] == "VALIDATION_ERROR"
        other.refresh_from_db()
        assert other.full_name == "Иванов Пётр"


_DOB = datetime.date(2015, 3, 12)


def _child_url(child: Student) -> str:
    return f"{CHILDREN_URL}{child.pk}/"


class TestChildDelete:
    def test_deletes_child_without_enrollments(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        child = StudentFactory(parent=parent)

        response = api_client.delete(_child_url(child))

        assert response.status_code == status.HTTP_204_NO_CONTENT
        child.refresh_from_db()
        assert child.archived_at is not None

    def test_history_is_kept(self, api_client: APIClient, parent: Parent) -> None:
        # Отменённый абонемент и прошедшее пробное — история, не помеха
        child = StudentFactory(parent=parent)
        canceled = EnrollmentFactory(student=child, status=EnrollmentStatus.CANCELED)
        past_trial = EnrollmentFactory(
            student=child,
            trial=True,
            trial_date=timezone.localdate() - datetime.timedelta(days=1),
        )

        response = api_client.delete(_child_url(child))

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert set(child.enrollments.values_list("pk", flat=True)) == {
            canceled.pk,
            past_trial.pk,
        }

    # ПОЧЕМУ: фабрика обязана брать «сегодня» по часам Django, а не ОС —
    # иначе вечером по Москве (в Новосибирске уже завтра) пробное «на сегодня»
    # считалось прошедшим. Зоны +14 и −12 расходятся с датой ОС в любой
    # момент суток хотя бы одна, поэтому тест ловит регресс без заморозки часов
    @pytest.mark.parametrize("tz_name", ["Pacific/Kiritimati", "Etc/GMT+12"])
    def test_todays_trial_blocks_delete_in_any_timezone(
        self, api_client: APIClient, parent: Parent, tz_name: str
    ) -> None:
        child = StudentFactory(parent=parent)

        with timezone.override(tz_name):
            enrollment = EnrollmentFactory(student=child, trial=True)
            response = api_client.delete(_child_url(child))

        assert enrollment.trial_date == timezone.localdate(
            timezone.now(), timezone=zoneinfo.ZoneInfo(tz_name)
        )
        assert response.status_code == status.HTTP_409_CONFLICT

    @pytest.mark.parametrize(
        ("kind", "enrollment_status"),
        [
            ("REGULAR", EnrollmentStatus.ENROLLED),
            ("REGULAR", EnrollmentStatus.HELD),
            ("TRIAL", EnrollmentStatus.HELD),
            ("TRIAL", EnrollmentStatus.ENROLLED),
        ],
    )
    def test_active_enrollment_blocks_delete(
        self,
        api_client: APIClient,
        parent: Parent,
        kind: str,
        enrollment_status: str,
    ) -> None:
        child = StudentFactory(parent=parent)
        enrollment = EnrollmentFactory(
            student=child,
            status=enrollment_status,
            trial=kind == "TRIAL",
            schedule=ScheduleFactory(
                activity=ActivityFactory(name="Робототехника"), group_name="Группа А"
            ),
        )

        response = api_client.delete(_child_url(child))

        assert response.status_code == status.HTTP_409_CONFLICT
        payload = response.json()
        assert payload["code"] == "CHILD_HAS_ACTIVE_ENROLLMENTS"
        (item,) = payload["extensions"]["active_enrollments"]
        assert item["id"] == enrollment.pk
        assert item["type"] == kind
        assert item["status"] == enrollment_status
        assert item["activity_name"] == "Робототехника"
        assert item["group_name"] == "Группа А"
        child.refresh_from_db()
        assert child.archived_at is None

    def test_409_payload_shape_for_subscription_and_trial(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        child = StudentFactory(parent=parent)
        regular = EnrollmentFactory(student=child)
        trial_date = timezone.localdate() + datetime.timedelta(days=3)
        trial = EnrollmentFactory(student=child, trial=True, trial_date=trial_date)

        response = api_client.delete(_child_url(child))

        items = response.json()["extensions"]["active_enrollments"]
        assert [item["id"] for item in items] == [regular.pk, trial.pk]
        assert items[0]["subscription_id"] == regular.subscription_id
        assert items[0]["trial_date"] is None
        assert items[1]["subscription_id"] is None
        assert items[1]["trial_date"] == trial_date.isoformat()

    def test_foreign_child_is_404(self, api_client: APIClient) -> None:
        foreign_child = StudentFactory()

        response = api_client.delete(_child_url(foreign_child))

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["code"] == "NOT_FOUND"
        foreign_child.refresh_from_db()
        assert foreign_child.archived_at is None

    def test_repeat_delete_is_404(self, api_client: APIClient, parent: Parent) -> None:
        child = StudentFactory(parent=parent)
        api_client.delete(_child_url(child))

        response = api_client.delete(_child_url(child))

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_requires_auth(self, parent: Parent) -> None:
        child = StudentFactory(parent=parent)

        response = APIClient().delete(_child_url(child))

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_allowed_before_onboarding_form(
        self, new_client: APIClient, new_parent: Parent
    ) -> None:
        child = StudentFactory(parent=new_parent)

        response = new_client.delete(_child_url(child))

        assert response.status_code == status.HTTP_204_NO_CONTENT

    def test_deleted_child_hidden_from_profile(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        kept = StudentFactory(parent=parent)
        deleted = StudentFactory(parent=parent)
        api_client.delete(_child_url(deleted))

        children = api_client.get(PROFILE_URL).json()["children"]

        assert [child["id"] for child in children] == [kept.pk]

    def test_deleted_child_cannot_be_edited(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        child = StudentFactory(parent=parent)
        api_client.delete(_child_url(child))

        response = api_client.patch(
            _child_url(child), {"school_grade": "6"}, format="json"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_same_child_can_be_added_again(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        payload = {"full_name": "Иванов Иван", "dob": _DOB.isoformat()}
        first_id = api_client.post(CHILDREN_URL, payload, format="json").json()["id"]
        api_client.delete(f"{CHILDREN_URL}{first_id}/")

        again = api_client.post(CHILDREN_URL, payload, format="json")

        assert again.status_code == status.HTTP_201_CREATED
        assert again.json()["id"] != first_id
        assert Student.objects.filter(parent=parent).count() == 2

    def test_rename_into_deleted_sibling_allowed(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        deleted = StudentFactory(parent=parent, full_name="Иванов Иван", dob=_DOB)
        api_client.delete(_child_url(deleted))
        other = StudentFactory(parent=parent, full_name="Иванов Пётр", dob=_DOB)

        response = api_client.patch(
            _child_url(other), {"full_name": "Иванов Иван"}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK


class TestSubscriptions:
    def test_lists_subscriptions_with_slots(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        student = StudentFactory(parent=parent, full_name="Иванов Иван")
        subscription = SubscriptionFactory(
            parent=parent, status=SubscriptionStatus.ACTIVE
        )
        schedule = ScheduleFactory(
            activity=ActivityFactory(name="Шахматы"),
            time_slot__day_of_week=5,
            time_slot__start_time=datetime.time(16, 0),
            time_slot__end_time=datetime.time(17, 0),
        )
        SubscriptionSlotFactory(
            subscription=subscription,
            slot_id=schedule.pk,
            granted_tokens=8,
            remaining_tokens=6,
        )
        EnrollmentFactory(student=student, subscription=subscription, schedule=schedule)
        SubscriptionFactory()

        response = api_client.get(SUBSCRIPTIONS_URL)

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert len(payload) == 1
        sub_data = payload[0]
        assert sub_data["id"] == subscription.pk
        assert sub_data["display_id"] == f"#SUB-{subscription.pk}"
        assert sub_data["status"] == "ACTIVE"
        assert sub_data["student_name"] == "Иванов Иван"
        assert sub_data["total_remaining"] == 6

        assert len(sub_data["slots"]) == 1
        slot = sub_data["slots"][0]
        assert slot["schedule_id"] == schedule.pk
        assert slot["activity_name"] == "Шахматы"
        assert slot["group_name"] == schedule.group_name
        assert slot["schedule"] == "СБ 16:00-17:00"
        assert slot["remaining_sessions"] == 6
        assert slot["total_sessions"] == 8

        # Убранные поля не должны возвращаться
        for removed_field in (
            "day_of_week",
            "start_time",
            "end_time",
            "student_id",
            "student_name",
        ):
            assert removed_field not in slot

    def test_canceled_enrollment_slot_not_counted_in_total_remaining(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        student = StudentFactory(parent=parent, full_name="Иванов Иван")
        subscription = SubscriptionFactory(
            parent=parent, status=SubscriptionStatus.ACTIVE
        )
        active_schedule = ScheduleFactory(activity=ActivityFactory(name="Шахматы"))
        canceled_schedule = ScheduleFactory(
            activity=ActivityFactory(name="Робототехника")
        )

        # Активный слот: 4 фишки
        SubscriptionSlotFactory(
            subscription=subscription,
            slot_id=active_schedule.pk,
            granted_tokens=4,
            remaining_tokens=4,
        )
        EnrollmentFactory(
            student=student,
            subscription=subscription,
            schedule=active_schedule,
            status=EnrollmentStatus.ENROLLED,
        )

        # Отменённый слот: в слоте БД осталось 2 фишки, но запись отменена
        SubscriptionSlotFactory(
            subscription=subscription,
            slot_id=canceled_schedule.pk,
            granted_tokens=4,
            remaining_tokens=2,
        )
        EnrollmentFactory(
            student=student,
            subscription=subscription,
            schedule=canceled_schedule,
            status=EnrollmentStatus.CANCELED,
        )

        response = api_client.get(SUBSCRIPTIONS_URL)

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert len(payload) == 1
        sub_data = payload[0]

        # В slots только активная запись
        assert len(sub_data["slots"]) == 1
        assert sub_data["slots"][0]["schedule_id"] == active_schedule.pk
        assert sub_data["slots"][0]["remaining_sessions"] == 4

        # total_remaining сходится с суммой по видимым слотам (4, а не 4 + 2 = 6)
        assert sub_data["total_remaining"] == 4


def _paid_subscription(
    parent: Parent,
    student: Student,
    *,
    slots: int = 1,
    created_days_ago: int = 0,
    expires_in_days: int = 20,
) -> Subscription:
    # Как после вебхука: ACTIVE, слоты с фишками, записи ENROLLED
    subscription = SubscriptionFactory(
        parent=parent,
        status=SubscriptionStatus.ACTIVE,
        start_date=timezone.localdate() - datetime.timedelta(days=10),
        expires_at=timezone.now() + datetime.timedelta(days=expires_in_days),
    )
    for _ in range(slots):
        schedule = ScheduleFactory()
        SubscriptionSlotFactory(
            subscription=subscription,
            slot_id=schedule.pk,
            granted_tokens=4,
            remaining_tokens=3,
        )
        EnrollmentFactory(student=student, subscription=subscription, schedule=schedule)
    Subscription.objects.filter(pk=subscription.pk).update(
        created_at=timezone.now() - datetime.timedelta(days=created_days_ago)
    )
    return subscription


class TestSubscriptionHistory:
    def test_expired_subscription_keeps_slots_and_child_name(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        student = StudentFactory(parent=parent, full_name="Иванов Иван")
        schedule = ScheduleFactory(
            activity=ActivityFactory(name="Шахматы"),
            time_slot__day_of_week=5,
            time_slot__start_time=datetime.time(16, 0),
            time_slot__end_time=datetime.time(17, 0),
        )
        subscription = SubscriptionFactory(
            parent=parent,
            status=SubscriptionStatus.ACTIVE,
            expires_at=timezone.now() - datetime.timedelta(days=1),
        )
        SubscriptionSlotFactory(
            subscription=subscription,
            slot_id=schedule.pk,
            granted_tokens=4,
            remaining_tokens=1,
        )
        EnrollmentFactory(student=student, subscription=subscription, schedule=schedule)
        # Настоящий свипер: EXPIRED, записи → CANCELED, остаток → депозит
        assert sweep_expired_subscriptions() == 1

        response = api_client.get(SUBSCRIPTIONS_URL)

        assert response.status_code == status.HTTP_200_OK
        (card,) = response.json()
        assert card["status"] == "EXPIRED"
        assert card["student_name"] == "Иванов Иван"
        (slot,) = card["slots"]
        assert slot["schedule_id"] == schedule.pk
        assert slot["activity_name"] == "Шахматы"
        assert slot["schedule"] == "СБ 16:00-17:00"
        assert slot["total_sessions"] == 4
        # Неиспользованная фишка ушла деньгами на депозит — в абонементе 0
        assert slot["remaining_sessions"] == 0
        assert card["total_remaining"] == 0

    def test_deleted_child_name_stays_in_history(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        student = StudentFactory(parent=parent, full_name="Петров Пётр")
        _paid_subscription(parent, student, expires_in_days=-1)
        sweep_expired_subscriptions()
        Student.objects.filter(pk=student.pk).update(archived_at=timezone.now())

        (card,) = api_client.get(SUBSCRIPTIONS_URL).json()

        assert card["student_name"] == "Петров Пётр"
        assert len(card["slots"]) == 1

    @pytest.mark.parametrize(
        "subscription_status",
        [
            SubscriptionStatus.DRAFT,
            SubscriptionStatus.PENDING,
            SubscriptionStatus.CANCELED,
        ],
    )
    def test_unpaid_orders_are_not_history(
        self, api_client: APIClient, parent: Parent, subscription_status: str
    ) -> None:
        # Незавершённая или брошенная оплата: слотов нет, деньги не пришли
        order = SubscriptionFactory(parent=parent, status=subscription_status)
        EnrollmentFactory(
            student=StudentFactory(parent=parent),
            subscription=order,
            status=EnrollmentStatus.CANCELED,
        )

        response = api_client.get(SUBSCRIPTIONS_URL)

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []

    def test_active_first_then_newest(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        student = StudentFactory(parent=parent)
        old_active = _paid_subscription(parent, student, created_days_ago=40)
        older_expired = _paid_subscription(
            parent, student, created_days_ago=90, expires_in_days=-30
        )
        newer_expired = _paid_subscription(
            parent, student, created_days_ago=60, expires_in_days=-1
        )
        assert sweep_expired_subscriptions() == 2
        new_active = _paid_subscription(parent, student, created_days_ago=1)

        ids = [card["id"] for card in api_client.get(SUBSCRIPTIONS_URL).json()]

        assert ids == [new_active.pk, old_active.pk, newer_expired.pk, older_expired.pk]


# Ручки ЛК со списками: без ?limit — массив, с ?limit — конверт
_PAGINATED_URLS = [
    SUBSCRIPTIONS_URL,
    TRIALS_URL,
    UPCOMING_URL,
    DEPOSIT_ENTRIES_URL,
    BOOKINGS_URL,
]


class TestCabinetPagination:
    @pytest.mark.parametrize("url", _PAGINATED_URLS)
    def test_without_limit_whole_list_as_array(
        self, api_client: APIClient, url: str
    ) -> None:
        response = api_client.get(url)

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []

    @pytest.mark.parametrize("url", _PAGINATED_URLS)
    def test_with_limit_envelope(self, api_client: APIClient, url: str) -> None:
        response = api_client.get(url, {"limit": 5})

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "count": 0,
            "next": None,
            "previous": None,
            "results": [],
        }

    @pytest.mark.parametrize("url", _PAGINATED_URLS)
    @pytest.mark.parametrize("query", ["limit=0", "limit=-3", "limit=2&offset=x"])
    def test_bad_limit_is_422(
        self, api_client: APIClient, url: str, query: str
    ) -> None:
        response = api_client.get(f"{url}?{query}")

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert response.json()["code"] == "VALIDATION_ERROR"

    def test_subscriptions_pages(self, api_client: APIClient, parent: Parent) -> None:
        student = StudentFactory(parent=parent)
        newest_first = [
            _paid_subscription(parent, student, created_days_ago=days).pk
            for days in (1, 2, 3, 4, 5)
        ]

        first = api_client.get(SUBSCRIPTIONS_URL, {"limit": 2}).json()
        last = api_client.get(SUBSCRIPTIONS_URL, {"limit": 2, "offset": 4}).json()

        assert first["count"] == 5
        assert [card["id"] for card in first["results"]] == newest_first[:2]
        assert first["previous"] is None
        assert "offset=2" in first["next"]
        assert [card["id"] for card in last["results"]] == newest_first[4:]
        assert last["next"] is None

    def test_limit_above_max_is_capped(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        # Сам потолок 60 проверен на галерее — класс общий; здесь только то,
        # что большой limit не ломает ручку ЛК
        student = StudentFactory(parent=parent)
        _paid_subscription(parent, student)

        body = api_client.get(SUBSCRIPTIONS_URL, {"limit": 1000}).json()

        assert body["count"] == 1
        assert len(body["results"]) == 1

    def test_subscriptions_query_count_does_not_grow_with_page(
        self,
        api_client: APIClient,
        parent: Parent,
        django_assert_num_queries: DjangoAssertNumQueries,
    ) -> None:
        student = StudentFactory(parent=parent)
        for days in range(6):
            _paid_subscription(parent, student, slots=2, created_days_ago=days)

        # COUNT + страница + слоты + записи (с ребёнком, группой, кружком JOIN'ом)
        # — и на 2 абонемента, и на 6; prefetch грузит только строки страницы
        with django_assert_num_queries(4):
            small = api_client.get(SUBSCRIPTIONS_URL, {"limit": 2})
        with django_assert_num_queries(4):
            big = api_client.get(SUBSCRIPTIONS_URL, {"limit": 6})
        # Без пагинации — те же запросы без COUNT
        with django_assert_num_queries(3):
            api_client.get(SUBSCRIPTIONS_URL)

        assert len(small.json()["results"]) == 2
        assert len(big.json()["results"]) == 6

    def test_trials_pages_and_query_count(
        self,
        api_client: APIClient,
        parent: Parent,
        django_assert_num_queries: DjangoAssertNumQueries,
    ) -> None:
        student = StudentFactory(parent=parent)
        today = timezone.localdate()
        trials = [
            EnrollmentFactory(
                student=student,
                trial=True,
                trial_date=today + datetime.timedelta(days=days),
            )
            for days in (1, 2, 3)
        ]
        for trial in trials:
            Transaction.objects.create(
                parent=parent,
                enrollment=trial,
                amount=50_000,
                status=TransactionStatus.SUCCEEDED,
            )

        # COUNT + страница + транзакции страницы
        with django_assert_num_queries(3):
            first = api_client.get(TRIALS_URL, {"limit": 1}).json()
        with django_assert_num_queries(3):
            rest = api_client.get(TRIALS_URL, {"limit": 3, "offset": 1}).json()

        assert first["count"] == 3
        assert [item["id"] for item in first["results"]] == [trials[2].pk]
        assert [item["id"] for item in rest["results"]] == [
            trials[1].pk,
            trials[0].pk,
        ]
        assert all(item["cost"] == 50_000 for item in rest["results"])

    def test_upcoming_pages_are_stable_for_same_time_sessions(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        # Два ребёнка в одной группе — занятия в одну минуту. Порядок обязан
        # быть однозначным, иначе одно занятие попадёт на две страницы
        schedule = ScheduleFactory()
        kids = [StudentFactory(parent=parent), StudentFactory(parent=parent)]
        # Записи создаём в обратном порядке id, чтобы порядок вставки не
        # совпал с ожидаемым случайно
        for kid in sorted(kids, key=lambda kid: -kid.pk):
            EnrollmentFactory(student=kid, schedule=schedule)

        pages = [
            api_client.get(UPCOMING_URL, {"weeks": 1, "limit": 1, "offset": offset})
            for offset in (0, 1)
        ]

        seen = [page.json()["results"][0]["student_id"] for page in pages]
        assert pages[0].json()["count"] == 2
        assert seen == sorted(kid.pk for kid in kids)

    def test_upcoming_pagination_keeps_filters(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        mine = StudentFactory(parent=parent)
        other = StudentFactory(parent=parent)
        EnrollmentFactory(student=mine, schedule=ScheduleFactory())
        EnrollmentFactory(student=other, schedule=ScheduleFactory())

        body = api_client.get(
            UPCOMING_URL, {"weeks": 4, "child_id": mine.pk, "limit": 3}
        ).json()

        assert body["count"] == 4
        assert len(body["results"]) == 3
        assert {item["student_id"] for item in body["results"]} == {mine.pk}
        assert "child_id=" in body["next"]
        assert "weeks=4" in body["next"]

    def test_deposit_entries_pages(self, api_client: APIClient, parent: Parent) -> None:
        TestDeposit()._history(parent)

        body = api_client.get(DEPOSIT_ENTRIES_URL, {"limit": 3, "offset": 3}).json()

        assert body["count"] == 4
        # Новые сверху: последняя строка — самая старая, начисление за абонемент
        (oldest,) = body["results"]
        assert oldest["reason"] == "SUBSCRIPTION_EXPIRY_CREDIT"


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
            datetime.datetime.strptime(item["date"], "%d.%m.%Y").weekday()
            == schedule.day_of_week
            for item in sessions
        )
        expected_time = (
            f"{schedule.start_time:%H:%M}-{schedule.end_time:%H:%M}"
            if schedule.end_time
            else f"{schedule.start_time:%H:%M}"
        )
        assert sessions[0]["time"] == expected_time

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
        assert first_session.strftime("%d.%m.%Y") not in dates

    def test_guest_event_matched_by_verified_email(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        event = EventFactory(
            title="Настольные игры",
            start_datetime=timezone.now() + datetime.timedelta(days=3),
        )
        _guest_registration(event, email=parent.email.upper())

        response = api_client.get(UPCOMING_URL)

        events = [item for item in response.json() if item["kind"] == "EVENT"]
        assert len(events) == 1
        assert events[0]["title"] == "Настольные игры"

    def test_event_not_matched_by_phone(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        # Телефон в анкете не подтверждается: вписав чужой номер, раньше
        # можно было увидеть чужие регистрации с именами детей
        event = EventFactory(start_datetime=timezone.now() + datetime.timedelta(days=3))
        _guest_registration(event, phone=str(parent.phone))
        EventRegistrationFactory(
            event=event, parent=ParentFactory(), phone=str(parent.phone)
        )

        response = api_client.get(UPCOMING_URL)

        assert [item for item in response.json() if item["kind"] == "EVENT"] == []

    def test_child_filter(self, api_client: APIClient, parent: Parent) -> None:
        first = StudentFactory(parent=parent)
        second = StudentFactory(parent=parent)
        EnrollmentFactory(student=first, schedule=ScheduleFactory())
        EnrollmentFactory(student=second, schedule=ScheduleFactory())

        response = api_client.get(UPCOMING_URL, {"child_id": first.pk, "weeks": 1})

        student_ids = {item["student_id"] for item in response.json()}
        assert student_ids == {first.pk}


class TestTrials:
    def test_trial_appears_in_list_with_cost_snapshot(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        student = StudentFactory(parent=parent)
        schedule = ScheduleFactory(activity=ActivityFactory(name="Английский язык"))
        trial = EnrollmentFactory(
            student=student,
            schedule=schedule,
            trial=True,
            trial_date=timezone.localdate() + datetime.timedelta(days=5),
        )
        Transaction.objects.create(
            parent=parent,
            enrollment=trial,
            amount=120_000,
            status=TransactionStatus.SUCCEEDED,
        )

        response = api_client.get(TRIALS_URL)

        assert response.status_code == status.HTTP_200_OK
        (item,) = response.json()
        assert item["id"] == trial.pk
        assert item["activity_name"] == "Английский язык"
        assert item["student_id"] == student.pk
        assert item["cost"] == 120_000
        assert item["status"] == "ENROLLED"

    def test_foreign_trial_is_invisible(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        EnrollmentFactory(trial=True)

        assert api_client.get(TRIALS_URL).json() == []

    def test_canceled_trial_is_hidden(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        student = StudentFactory(parent=parent)
        EnrollmentFactory(student=student, trial=True, status="CANCELED")

        assert api_client.get(TRIALS_URL).json() == []


class TestUpcomingTrials:
    def test_paid_trial_lands_in_feed_with_kind(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        student = StudentFactory(parent=parent)
        trial_date = timezone.localdate() + datetime.timedelta(days=3)
        EnrollmentFactory(student=student, trial=True, trial_date=trial_date)

        response = api_client.get(UPCOMING_URL)

        trials = [item for item in response.json() if item["kind"] == "TRIAL"]
        assert len(trials) == 1
        assert trials[0]["date"] == trial_date.strftime("%d.%m.%Y")
        assert trials[0]["source_type"] == "trial"
        assert trials[0]["student_id"] == student.pk

    def test_unpaid_hold_is_not_shown(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        student = StudentFactory(parent=parent)
        EnrollmentFactory(
            student=student,
            trial=True,
            status="HELD",
            trial_date=timezone.localdate() + datetime.timedelta(days=3),
        )

        response = api_client.get(UPCOMING_URL)

        assert [item for item in response.json() if item["kind"] == "TRIAL"] == []

    def test_trial_survives_child_filter(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        student = StudentFactory(parent=parent)
        EnrollmentFactory(
            student=student,
            trial=True,
            trial_date=timezone.localdate() + datetime.timedelta(days=2),
        )

        response = api_client.get(UPCOMING_URL, {"child_id": student.pk})

        assert [item["kind"] for item in response.json()] == ["TRIAL"]


def _guest_registration(
    event: Event, *, email: str = "", phone: str = "+79130000000"
) -> EventRegistration:
    # Регистрация с сайта без входа — parent не заполнен
    return register_for_event(
        event.pk,
        RegistrationSubmission(
            child_name="Иван",
            parent_name="Ольга",
            phone=phone,
            email=email,
            attendees_count=1,
            source="",
            comment="",
        ),
    )


def _local_moment(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    return timezone.make_aware(
        datetime.datetime.combine(day, datetime.time(hour, minute))
    )


def _paid_trial(
    student: Student,
    *,
    days: int,
    cost: int = 50_000,
    status: str = "ENROLLED",
    **extra: object,
) -> Enrollment:
    trial = EnrollmentFactory(
        student=student,
        trial=True,
        status=status,
        trial_date=timezone.localdate() + datetime.timedelta(days=days),
        **extra,
    )
    Transaction.objects.create(
        parent=student.parent,
        enrollment=trial,
        amount=cost,
        status=(
            TransactionStatus.CANCELED
            if status == "CANCELED"
            else TransactionStatus.SUCCEEDED
        ),
    )
    return trial


def _my_event(
    parent: Parent, *, days: int, hour: int = 11, **extra: object
) -> EventRegistration:
    event = EventFactory(
        start_datetime=_local_moment(
            timezone.localdate() + datetime.timedelta(days=days), hour
        )
    )
    return EventRegistrationFactory(
        event=event,
        parent=parent,
        **{"status": RegistrationStatus.CONFIRMED, **extra},
    )


def _keys(items: list[dict[str, object]]) -> list[tuple[object, object]]:
    return [(item["kind"], item["id"]) for item in items]


class TestBookings:
    def test_trial_card(self, api_client: APIClient, parent: Parent) -> None:
        student = StudentFactory(parent=parent, full_name="Иванов Иван")
        schedule = ScheduleFactory(
            activity=ActivityFactory(name="Английский язык"),
            group_name="Начинающие",
        )
        trial = _paid_trial(student, days=3, cost=120_000, schedule=schedule)

        (card,) = api_client.get(BOOKINGS_URL).json()

        assert card == {
            "kind": "TRIAL",
            "id": trial.pk,
            "title": "Английский язык",
            "group_name": "Начинающие",
            "date": trial.trial_date.isoformat(),
            "start_time": schedule.start_time.strftime("%H:%M"),
            "end_time": schedule.end_time.strftime("%H:%M"),
            "cost": 120_000,
            "child_name": "Иванов Иван",
            "student_id": student.pk,
            "attendees_count": None,
            "status": "CONFIRMED",
            "status_display": "Записан",
            "is_past": False,
            "activity_id": schedule.activity_id,
            "event_id": None,
        }

    def test_event_card_in_local_time_with_total_cost(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        day = timezone.localdate() + datetime.timedelta(days=4)
        event = EventFactory(
            title="Театральные игры",
            start_datetime=_local_moment(day, 11),
            duration_minutes=90,
            price=60_000,
        )
        registration = EventRegistrationFactory(
            event=event,
            parent=parent,
            child_name="Ваня",
            attendees_count=2,
            status=RegistrationStatus.PENDING_PAYMENT,
        )

        (card,) = api_client.get(BOOKINGS_URL).json()

        assert card == {
            "kind": "EVENT",
            "id": registration.pk,
            "title": "Театральные игры",
            "group_name": None,
            "date": day.isoformat(),
            "start_time": "11:00",
            "end_time": "12:30",
            "cost": 120_000,
            "child_name": "Ваня",
            "student_id": None,
            "attendees_count": 2,
            "status": "PENDING",
            "status_display": "Ожидает оплаты",
            "is_past": False,
            "activity_id": None,
            "event_id": event.pk,
        }

    def test_free_event_costs_zero_and_new_awaits_confirmation(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        _my_event(parent, days=2, status=RegistrationStatus.NEW)

        (card,) = api_client.get(BOOKINGS_URL).json()

        assert card["cost"] == 0
        assert card["status"] == "PENDING"
        assert card["status_display"] == "Ожидает подтверждения"

    def test_unpaid_trial_hold_is_pending(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        _paid_trial(StudentFactory(parent=parent), days=2, status="HELD")

        (card,) = api_client.get(BOOKINGS_URL).json()

        assert (card["status"], card["status_display"]) == (
            "PENDING",
            "Ожидает оплаты",
        )

    def test_mixed_feed_sorted_by_date(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        student = StudentFactory(parent=parent)
        trial_in_3 = _paid_trial(student, days=3)
        trial_in_5 = _paid_trial(student, days=5)
        event_in_1 = _my_event(parent, days=1)
        event_in_4 = _my_event(parent, days=4)

        items = api_client.get(BOOKINGS_URL).json()

        assert _keys(items) == [
            ("EVENT", event_in_1.pk),
            ("TRIAL", trial_in_3.pk),
            ("EVENT", event_in_4.pk),
            ("TRIAL", trial_in_5.pk),
        ]

    def test_same_day_sorted_by_time(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        late = _my_event(parent, days=2, hour=18)
        early = _my_event(parent, days=2, hour=9)

        items = api_client.get(BOOKINGS_URL).json()

        assert _keys(items) == [("EVENT", early.pk), ("EVENT", late.pk)]

    def test_periods(self, api_client: APIClient, parent: Parent) -> None:
        student = StudentFactory(parent=parent)
        future_trial = _paid_trial(student, days=2)
        future_event = _my_event(parent, days=1)
        old_trial = _paid_trial(student, days=-10)
        recent_event = _my_event(parent, days=-2)

        upcoming = api_client.get(BOOKINGS_URL).json()
        past = api_client.get(BOOKINGS_URL, {"period": "past"}).json()
        everything = api_client.get(BOOKINGS_URL, {"period": "all"}).json()

        future = [("EVENT", future_event.pk), ("TRIAL", future_trial.pk)]
        # Прошедшие — свежие сверху
        history = [("EVENT", recent_event.pk), ("TRIAL", old_trial.pk)]
        assert _keys(upcoming) == future
        assert _keys(past) == history
        assert _keys(everything) == future + history
        assert [item["is_past"] for item in everything] == [False, False, True, True]

    def test_today_counts_as_upcoming_until_midnight(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        # Событие сегодня в 00:30 уже началось, но «прошло» считаем по дате
        event = EventFactory(start_datetime=_local_moment(timezone.localdate(), 0, 30))
        registration = EventRegistrationFactory(event=event, parent=parent)
        trial = _paid_trial(StudentFactory(parent=parent), days=0)

        upcoming = api_client.get(BOOKINGS_URL).json()
        past = api_client.get(BOOKINGS_URL, {"period": "past"}).json()

        assert set(_keys(upcoming)) == {
            ("EVENT", registration.pk),
            ("TRIAL", trial.pk),
        }
        assert past == []

    def test_kind_filter(self, api_client: APIClient, parent: Parent) -> None:
        trial = _paid_trial(StudentFactory(parent=parent), days=2)
        registration = _my_event(parent, days=2)

        trials = api_client.get(BOOKINGS_URL, {"kind": "TRIAL"}).json()
        events = api_client.get(BOOKINGS_URL, {"kind": "EVENT"}).json()

        assert _keys(trials) == [("TRIAL", trial.pk)]
        assert _keys(events) == [("EVENT", registration.pk)]

    @pytest.mark.parametrize(
        ("field", "value"),
        [("period", "future"), ("period", "UPCOMING"), ("kind", "trial")],
    )
    def test_bad_filter_is_422(
        self, api_client: APIClient, field: str, value: str
    ) -> None:
        response = api_client.get(BOOKINGS_URL, {field: value})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        body = response.json()
        assert body["code"] == "VALIDATION_ERROR"
        assert [p["name"] for p in body["extensions"]["invalid_params"]] == [field]

    def test_empty_filter_means_default(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        _paid_trial(StudentFactory(parent=parent), days=-3)

        response = api_client.get(f"{BOOKINGS_URL}?period=&kind=")

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []

    def test_pages_keep_order_and_filters(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        student = StudentFactory(parent=parent)
        expected = []
        for days in (1, 2, 3):
            expected.append(("EVENT", _my_event(parent, days=days).pk))
            expected.append(("TRIAL", _paid_trial(student, days=days + 10).pk))
        # События (через 1-3 дня) раньше всех пробных (через 11-13 дней)
        expected.sort(key=lambda key: key[0] == "TRIAL")

        first = api_client.get(BOOKINGS_URL, {"period": "all", "limit": 4}).json()
        second = api_client.get(first["next"]).json()

        assert first["count"] == 6
        assert "period=all" in first["next"]
        assert _keys(first["results"] + second["results"]) == expected
        assert second["next"] is None

    def test_foreign_bookings_are_invisible(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        _paid_trial(StudentFactory(), days=2)
        _my_event(ParentFactory(), days=2)

        assert api_client.get(BOOKINGS_URL, {"period": "all"}).json() == []

    def test_same_phone_other_parent_is_invisible(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        event = EventFactory()
        EventRegistrationFactory(
            event=event, parent=ParentFactory(), phone=str(parent.phone)
        )
        _guest_registration(event, phone=str(parent.phone))

        assert api_client.get(BOOKINGS_URL, {"period": "all"}).json() == []

    def test_guest_registration_found_by_email(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        event = EventFactory()
        mine = _guest_registration(event, email=parent.email.upper())
        # Чужую регистрацию с моим email не показываем: у неё есть хозяин
        EventRegistrationFactory(
            event=event, parent=ParentFactory(), email=parent.email
        )
        # Гость без email ни с кем не совпадает
        _guest_registration(event, email="")

        items = api_client.get(BOOKINGS_URL).json()

        assert _keys(items) == [("EVENT", mine.pk)]

    def test_canceled_are_hidden(self, api_client: APIClient, parent: Parent) -> None:
        _paid_trial(StudentFactory(parent=parent), days=2, status="CANCELED")
        _my_event(parent, days=2, status=RegistrationStatus.CANCELED)

        assert api_client.get(BOOKINGS_URL, {"period": "all"}).json() == []

    def test_deleted_child_trial_stays(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        student = StudentFactory(parent=parent, full_name="Иванов Иван")
        _paid_trial(student, days=-5)
        student.archived_at = timezone.now()
        student.save(update_fields=["archived_at"])

        (card,) = api_client.get(BOOKINGS_URL, {"period": "past"}).json()

        assert card["child_name"] == "Иванов Иван"

    def test_query_count_is_fixed(
        self,
        api_client: APIClient,
        parent: Parent,
        django_assert_num_queries: DjangoAssertNumQueries,
    ) -> None:
        student = StudentFactory(parent=parent)
        _paid_trial(student, days=1)
        _my_event(parent, days=1)

        # пробные + их транзакции + регистрации (с событием JOIN'ом)
        with django_assert_num_queries(3):
            api_client.get(BOOKINGS_URL)

        for days in range(2, 7):
            _paid_trial(StudentFactory(parent=parent), days=days)
            _my_event(parent, days=days)

        with django_assert_num_queries(3):
            small = api_client.get(BOOKINGS_URL, {"limit": 2}).json()
        with django_assert_num_queries(3):
            whole = api_client.get(BOOKINGS_URL).json()
        # Ненужную таблицу не трогаем
        with django_assert_num_queries(1):
            api_client.get(BOOKINGS_URL, {"kind": "EVENT"})

        assert small["count"] == 12
        assert len(whole) == 12


class TestDeposit:
    def _history(self, parent: Parent) -> ParentDeposit:
        # Реальный сценарий: остаток абонемента пришёл на депозит, часть
        # потрачена на новый абонемент, неоплаченный заказ вернул деньги
        deposit = ParentDeposit.objects.create(parent=parent, balance=250_000)
        expired = SubscriptionFactory(parent=parent)
        bought = SubscriptionFactory(parent=parent)
        abandoned = SubscriptionFactory(parent=parent)
        spend_tx = Transaction.objects.create(
            parent=parent,
            subscription=bought,
            amount=0,
            status=TransactionStatus.SUCCEEDED,
        )
        return_tx = Transaction.objects.create(
            parent=parent,
            subscription=abandoned,
            amount=100_000,
            status=TransactionStatus.CANCELED,
        )
        base = timezone.now() - datetime.timedelta(days=10)
        rows = [
            (
                300_000,
                DepositEntryReason.SUBSCRIPTION_EXPIRY_CREDIT,
                {"subscription": expired},
            ),
            (-50_000, DepositEntryReason.CHECKOUT_SPEND, {"transaction": spend_tx}),
            (-20_000, DepositEntryReason.CHECKOUT_SPEND, {"transaction": return_tx}),
            (
                20_000,
                DepositEntryReason.ORDER_CANCELED_RETURN,
                {"transaction": return_tx},
            ),
        ]
        for day, (amount, reason, link) in enumerate(rows):
            entry = DepositEntry.objects.create(
                deposit=deposit, amount=amount, reason=reason, **link
            )
            DepositEntry.objects.filter(pk=entry.pk).update(
                created_at=base + datetime.timedelta(days=day)
            )
        return deposit

    def test_requires_auth(self) -> None:
        assert APIClient().get(DEPOSIT_URL).status_code == 401
        assert APIClient().get(DEPOSIT_ENTRIES_URL).status_code == 401

    def test_parent_without_deposit_gets_zero_and_empty_history(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        balance = api_client.get(DEPOSIT_URL)
        entries = api_client.get(DEPOSIT_ENTRIES_URL)

        assert balance.status_code == status.HTTP_200_OK
        assert balance.json() == {"balance": 0}
        assert entries.status_code == status.HTTP_200_OK
        assert entries.json() == []
        # Чтение не заводит строку депозита — это делает только начисление
        assert not ParentDeposit.objects.filter(parent=parent).exists()

    def test_returns_balance(self, api_client: APIClient, parent: Parent) -> None:
        self._history(parent)

        response = api_client.get(DEPOSIT_URL)

        assert response.json() == {"balance": 250_000}

    def test_history_newest_first_with_subscription_links(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        deposit = self._history(parent)

        response = api_client.get(DEPOSIT_ENTRIES_URL)

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert [item["amount"] for item in payload] == [
            20_000,
            -20_000,
            -50_000,
            300_000,
        ]
        # Инвариант депозита: баланс равен сумме журнала
        assert sum(item["amount"] for item in payload) == deposit.balance
        returned, _, spent, credited = payload
        assert returned["reason"] == "ORDER_CANCELED_RETURN"
        assert returned["reason_display"] == "Возврат: заказ не был оплачен"
        assert credited["reason_display"] == "Несгораемый остаток абонемента"
        # Абонемент находится и напрямую, и через транзакцию чекаута
        expired_sub = DepositEntry.objects.get(
            reason=DepositEntryReason.SUBSCRIPTION_EXPIRY_CREDIT
        ).subscription_id
        spend_sub = Transaction.objects.get(amount=0).subscription_id
        assert credited["subscription_id"] == expired_sub
        assert credited["subscription_display_id"] == f"#SUB-{expired_sub}"
        assert spent["subscription_id"] == spend_sub
        assert set(spent) == {
            "id",
            "amount",
            "reason",
            "reason_display",
            "subscription_id",
            "subscription_display_id",
            "created_at",
        }

    def test_foreign_deposit_is_invisible(
        self, api_client: APIClient, parent: Parent
    ) -> None:
        self._history(ParentFactory())

        assert api_client.get(DEPOSIT_URL).json() == {"balance": 0}
        assert api_client.get(DEPOSIT_ENTRIES_URL).json() == []

    def test_balance_is_single_query(
        self,
        api_client: APIClient,
        parent: Parent,
        django_assert_num_queries: DjangoAssertNumQueries,
    ) -> None:
        self._history(parent)

        with django_assert_num_queries(1):
            api_client.get(DEPOSIT_URL)

    def test_history_query_count_does_not_grow(
        self,
        api_client: APIClient,
        parent: Parent,
        django_assert_num_queries: DjangoAssertNumQueries,
    ) -> None:
        self._history(parent)

        # Один запрос на весь журнал: абонемент-через-транзакцию склеивается
        # JOIN'ом, а не догрузкой на каждую строку
        with django_assert_num_queries(1):
            api_client.get(DEPOSIT_ENTRIES_URL)


CHECKOUT_SUBSCRIPTION_URL = "/api/v1/checkout/subscription"
CHECKOUT_TRIAL_URL = "/api/v1/checkout/trial"
PROFILE_INCOMPLETE_TYPE = "urn:problem-type:profileincomplete"

# Ручки, закрытые до заполнения анкеты: (метод, URL)
_GATED_ENDPOINTS = [
    ("get", SUBSCRIPTIONS_URL),
    ("get", TRIALS_URL),
    ("get", UPCOMING_URL),
    ("get", DEPOSIT_URL),
    ("get", DEPOSIT_ENTRIES_URL),
    ("get", BOOKINGS_URL),
    ("post", CHECKOUT_SUBSCRIPTION_URL),
    ("post", CHECKOUT_TRIAL_URL),
]

_ANKETA: dict[str, object] = {
    "full_name": "Петрова Анна Сергеевна",
    "phone": "+79131234567",
    "referral_source": "MAPS",
    "pd_consent": True,
}


@pytest.fixture
def new_parent() -> Parent:
    # Так выглядит родитель сразу после первого входа
    return ParentFactory(full_name="", phone="", referral_source="", pd_consent_at=None)


@pytest.fixture
def new_client(new_parent: Parent) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=new_parent)
    return client


class TestOnboardingGate:
    @pytest.mark.parametrize(("method", "url"), _GATED_ENDPOINTS)
    def test_cabinet_and_checkout_closed_until_form_filled(
        self, new_client: APIClient, method: str, url: str
    ) -> None:
        response = getattr(new_client, method)(url, {}, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN
        body = response.json()
        # Машинный признак, по которому фронт отличает анкету от «чужого»
        assert body["type"] == PROFILE_INCOMPLETE_TYPE
        assert body["title"] == "ProfileIncomplete"

    @pytest.mark.parametrize(("method", "url"), _GATED_ENDPOINTS)
    def test_guest_still_gets_401_not_403(self, method: str, url: str) -> None:
        response = getattr(APIClient(), method)(url, {}, format="json")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_profile_readable_and_reports_incomplete(
        self, new_client: APIClient, new_parent: Parent
    ) -> None:
        response = new_client.get(PROFILE_URL)

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["profile_completed"] is False
        assert payload["email"] == new_parent.email
        assert payload["referral_source"] == ""

    def test_children_can_be_added_and_edited_before_form(
        self, new_client: APIClient, new_parent: Parent
    ) -> None:
        created = new_client.post(
            CHILDREN_URL,
            {"full_name": "Петров Миша", "dob": "2016-05-01"},
            format="json",
        )
        assert created.status_code == status.HTTP_201_CREATED

        updated = new_client.patch(
            f"{CHILDREN_URL}{created.json()['id']}/",
            {"school_grade": "3"},
            format="json",
        )
        assert updated.status_code == status.HTTP_200_OK

    def test_filled_form_opens_everything(
        self, new_client: APIClient, new_parent: Parent
    ) -> None:
        response = new_client.patch(PROFILE_URL, _ANKETA, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["profile_completed"] is True
        new_parent.refresh_from_db()
        # force_authenticate держит тот же объект, что обновил PATCH
        for url in (SUBSCRIPTIONS_URL, TRIALS_URL, UPCOMING_URL, DEPOSIT_URL):
            assert new_client.get(url).status_code == status.HTTP_200_OK
        # Чекаут пускает дальше анкеты: без Idempotency-Key — уже валидация
        checkout = new_client.post(CHECKOUT_TRIAL_URL, {}, format="json")
        assert checkout.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_form_can_be_saved_in_parts(
        self, new_client: APIClient, new_parent: Parent
    ) -> None:
        first = new_client.patch(
            PROFILE_URL, {"full_name": _ANKETA["full_name"]}, format="json"
        )
        assert first.json()["profile_completed"] is False

        rest = new_client.patch(
            PROFILE_URL,
            {
                "phone": _ANKETA["phone"],
                "referral_source": "FRIENDS",
                "pd_consent": True,
            },
            format="json",
        )
        assert rest.json()["profile_completed"] is True

    @pytest.mark.parametrize(
        "payload",
        [
            {"full_name": ""},
            {"full_name": "   "},
            {"phone": ""},
            {"referral_source": ""},
            {"referral_source": "UNKNOWN"},
            {"referral_source": "TIKTOK"},
        ],
    )
    def test_required_fields_cannot_be_blanked_or_faked(
        self, api_client: APIClient, parent: Parent, payload: dict[str, str]
    ) -> None:
        # ПОЧЕМУ: иначе родитель одним PATCH запер бы себе ЛК, а UNKNOWN
        # зарезервирован за миграцией старых родителей
        response = api_client.patch(PROFILE_URL, payload, format="json")

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        parent.refresh_from_db()
        assert parent.is_profile_completed is True

    def test_staff_without_phone_is_not_exempt(self) -> None:
        # ПОЧЕМУ: у педагогов и админов своя работа в админке (сессия, не DRF);
        # если педагог сам покупает ребёнку кружок — заполняет анкету как все
        teacher = ParentFactory(phone="", is_staff=True)
        client = APIClient()
        client.force_authenticate(user=teacher)

        assert client.get(SUBSCRIPTIONS_URL).status_code == status.HTTP_403_FORBIDDEN
        assert client.get(PROFILE_URL).status_code == status.HTTP_200_OK


class TestReferralBackfillMigration:
    def test_existing_parents_marked_unknown_new_fields_untouched(self) -> None:
        import importlib

        from django.apps import apps as django_apps

        migration = importlib.import_module(
            "apps.users.migrations.0005_parent_referral_source"
        )
        legacy = ParentFactory(referral_source="")
        answered = ParentFactory(referral_source="SCHOOL")

        migration.mark_existing_parents_unknown(django_apps, None)

        legacy.refresh_from_db()
        answered.refresh_from_db()
        assert legacy.referral_source == "UNKNOWN"
        assert legacy.is_profile_completed is True
        assert answered.referral_source == "SCHOOL"


class TestRegistrationConsent:
    def test_filled_fields_without_consent_keep_cabinet_closed(
        self, new_client: APIClient
    ) -> None:
        anketa = {k: v for k, v in _ANKETA.items() if k != "pd_consent"}

        response = new_client.patch(PROFILE_URL, anketa, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["profile_completed"] is False
        assert response.json()["pd_consent_at"] is None
        assert new_client.get(SUBSCRIPTIONS_URL).status_code == 403

    def test_unchecked_box_is_rejected(self, new_client: APIClient) -> None:
        response = new_client.patch(
            PROFILE_URL, {**_ANKETA, "pd_consent": False}, format="json"
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert PersonalDataConsent.objects.count() == 0

    def test_consent_is_journaled_as_proof(
        self, new_client: APIClient, new_parent: Parent
    ) -> None:
        response = new_client.patch(
            PROFILE_URL,
            _ANKETA,
            format="json",
            REMOTE_ADDR="203.0.113.7",
            HTTP_USER_AGENT="Mozilla/5.0 (Android)",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["pd_consent_at"] is not None
        record = PersonalDataConsent.objects.get()
        assert record.purpose == ConsentPurpose.REGISTRATION
        assert record.document_version == settings.PD_CONSENT_VERSION
        assert record.parent == new_parent
        assert record.email == new_parent.email
        # Телефон из этой же анкеты — снимок на момент согласия
        assert str(record.phone) == _ANKETA["phone"]
        assert record.ip == "203.0.113.7"
        assert record.user_agent == "Mozilla/5.0 (Android)"

    def test_repeated_consent_does_not_duplicate_or_move_date(
        self, new_client: APIClient, new_parent: Parent
    ) -> None:
        new_client.patch(PROFILE_URL, _ANKETA, format="json")
        new_parent.refresh_from_db()
        first_consent_at = new_parent.pd_consent_at

        new_client.patch(PROFILE_URL, {"pd_consent": True}, format="json")

        new_parent.refresh_from_db()
        assert new_parent.pd_consent_at == first_consent_at
        assert PersonalDataConsent.objects.count() == 1

    def test_consent_ip_taken_through_own_proxy(self, new_client: APIClient) -> None:
        with override_settings(
            REST_FRAMEWORK={**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}
        ):
            new_client.patch(
                PROFILE_URL,
                _ANKETA,
                format="json",
                REMOTE_ADDR="172.18.0.2",
                HTTP_X_FORWARDED_FOR="1.2.3.4, 203.0.113.7",
            )

        assert PersonalDataConsent.objects.get().ip == "203.0.113.7"

    def test_admin_cannot_fake_or_erase_consent(self) -> None:
        # ПОЧЕМУ: согласие — действие самого родителя. Даже суперпользователь
        # не ставит галочку «за клиента» и не правит/удаляет журнал
        from django.contrib import admin
        from django.test import RequestFactory

        from apps.users.admin import ParentAdmin

        request = RequestFactory().get("/admin/")
        request.user = ParentFactory(is_staff=True, is_superuser=True)
        journal_admin = admin.site._registry[PersonalDataConsent]

        assert "pd_consent_at" in ParentAdmin.readonly_fields
        assert journal_admin.has_add_permission(request) is False
        assert journal_admin.has_change_permission(request) is False
        assert journal_admin.has_delete_permission(request) is False
