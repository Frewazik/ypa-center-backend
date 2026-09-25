from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

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
from apps.billing.models import (
    DepositEntry,
    DepositEntryReason,
    EnrollmentStatus,
    ParentDeposit,
    SubscriptionStatus,
    Transaction,
    TransactionStatus,
)
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
        assert trials[0]["date"] == trial_date.isoformat()
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
