from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.billing.services import create_trial_payment
from apps.billing.tests.test_billing import FakeGateway
from apps.catalog.models import Activity
from apps.schedule.models import MaskType, Schedule
from apps.schedule.ports import DjangoSchedulePort
from apps.schedule.services import is_lesson_bookable, list_trial_slots
from apps.schedule.tests.factories import (
    ActivityFactory,
    EnrollmentFactory,
    ParentFactory,
    ScheduleFactory,
    ScheduleMaskFactory,
    StudentFactory,
    TimeSlotFactory,
)

if TYPE_CHECKING:
    from pytest_django import DjangoAssertNumQueries

pytestmark = pytest.mark.django_db

# ПОЧЕМУ: «сейчас» задаётся явно, а даты — от будущего понедельника: маски
# и пробные в прошлом бессмысленны, а фиксированная дата в прошлом
# сломала бы сквозные проверки через чекаут
_TODAY = datetime.date.today()
MONDAY = _TODAY + datetime.timedelta(days=7 - _TODAY.weekday())
WEDNESDAY = MONDAY + datetime.timedelta(days=2)
FRIDAY = MONDAY + datetime.timedelta(days=4)
SATURDAY = MONDAY + datetime.timedelta(days=5)
SUNDAY = MONDAY + datetime.timedelta(days=6)
WEEK = datetime.timedelta(weeks=1)


def _at(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    return timezone.make_aware(
        datetime.datetime.combine(day, datetime.time(hour, minute))
    )


def _group(
    activity: Activity,
    day_of_week: int,
    start: datetime.time,
    end: datetime.time | None = None,
    **kwargs: object,
) -> Schedule:
    finish = end if end is not None else datetime.time(start.hour + 1, start.minute)
    time_slot = TimeSlotFactory(
        day_of_week=day_of_week, start_time=start, end_time=finish
    )
    group: Schedule = ScheduleFactory(activity=activity, time_slot=time_slot, **kwargs)
    return group


def _slots_url(activity_id: int) -> str:
    return f"/api/v1/public/activities/{activity_id}/next-slots/"


@pytest.fixture(autouse=True)
def _isolated_cache() -> Iterator[None]:
    with override_settings(
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": f"trial-slots-{uuid.uuid4()}",
            }
        }
    ):
        yield


@pytest.fixture
def activity() -> Activity:
    chess: Activity = ActivityFactory(name="Шахматы")
    return chess


class TestListTrialSlots:
    def test_two_weeks_from_today_sorted_by_date_and_time(
        self, activity: Activity
    ) -> None:
        wednesday = _group(activity, 2, datetime.time(17))
        friday = _group(activity, 4, datetime.time(17))
        saturday = _group(activity, 5, datetime.time(10))

        result = list_trial_slots(activity.pk, now=_at(MONDAY, 9))

        assert (result.date_from, result.date_to) == (MONDAY, SUNDAY + WEEK)
        assert [(slot.schedule_id, slot.date) for slot in result.slots] == [
            (wednesday.pk, WEDNESDAY),
            (friday.pk, FRIDAY),
            (saturday.pk, SATURDAY),
            (wednesday.pk, WEDNESDAY + WEEK),
            (friday.pk, FRIDAY + WEEK),
            (saturday.pk, SATURDAY + WEEK),
        ]

    def test_window_spanning_three_calendar_weeks(self, activity: Activity) -> None:
        # Воскресенье: окно задевает свою неделю, следующую и ещё одну
        sunday = _group(activity, 6, datetime.time(16))
        monday = _group(activity, 0, datetime.time(16))

        result = list_trial_slots(activity.pk, now=_at(SUNDAY, 9))

        assert (result.date_from, result.date_to) == (
            SUNDAY,
            SUNDAY + datetime.timedelta(days=13),
        )
        assert [(slot.schedule_id, slot.date) for slot in result.slots] == [
            (sunday.pk, SUNDAY),
            (monday.pk, MONDAY + WEEK),
            (sunday.pk, SUNDAY + WEEK),
            (monday.pk, MONDAY + 2 * WEEK),
        ]

    def test_full_group_is_not_listed(self, activity: Activity) -> None:
        full = _group(activity, 2, datetime.time(17), max_capacity=1)
        free = _group(activity, 4, datetime.time(17), max_capacity=1)
        EnrollmentFactory(schedule=full)

        result = list_trial_slots(activity.pk, now=_at(MONDAY, 9))

        assert {slot.schedule_id for slot in result.slots} == {free.pk}

    def test_trial_takes_seat_only_on_its_date(self, activity: Activity) -> None:
        group = _group(activity, 2, datetime.time(17), max_capacity=1)
        EnrollmentFactory(schedule=group, trial=True, trial_date=WEDNESDAY)

        result = list_trial_slots(activity.pk, now=_at(MONDAY, 9))

        assert [slot.date for slot in result.slots] == [WEDNESDAY + WEEK]

    def test_cancelled_lesson_is_not_listed(self, activity: Activity) -> None:
        group = _group(activity, 2, datetime.time(17))
        ScheduleMaskFactory(schedule=group, target_date=WEDNESDAY)

        result = list_trial_slots(activity.pk, now=_at(MONDAY, 9))

        assert [slot.date for slot in result.slots] == [WEDNESDAY + WEEK]

    def test_rescheduled_lesson_moves_to_new_date_and_time(
        self, activity: Activity
    ) -> None:
        group = _group(activity, 2, datetime.time(17))
        ScheduleMaskFactory(
            schedule=group,
            target_date=WEDNESDAY,
            type=MaskType.RESCHEDULE,
            new_day_of_week=3,
            new_start_time=datetime.time(18, 30),
            new_end_time=datetime.time(19, 30),
        )

        result = list_trial_slots(activity.pk, now=_at(MONDAY, 9))

        moved, regular = result.slots
        assert moved.date == WEDNESDAY + datetime.timedelta(days=1)
        assert (moved.start_time, moved.end_time) == (
            datetime.time(18, 30),
            datetime.time(19, 30),
        )
        assert moved.is_rescheduled is True
        assert (regular.date, regular.start_time) == (
            WEDNESDAY + WEEK,
            datetime.time(17),
        )
        assert regular.is_rescheduled is False

    def test_started_lesson_today_is_not_listed(self, activity: Activity) -> None:
        morning = _group(activity, 0, datetime.time(10))
        noon = _group(activity, 0, datetime.time(12))
        evening = _group(activity, 0, datetime.time(14))

        result = list_trial_slots(activity.pk, now=_at(MONDAY, 12))

        today = [slot.schedule_id for slot in result.slots if slot.date == MONDAY]
        assert today == [evening.pk]
        next_week = {
            slot.schedule_id for slot in result.slots if slot.date == MONDAY + WEEK
        }
        assert next_week == {morning.pk, noon.pk, evening.pk}

    def test_started_check_uses_rescheduled_time(self, activity: Activity) -> None:
        # Занятие в 10:00 перенесли на 15:00 того же дня — в полдень ещё можно
        group = _group(activity, 0, datetime.time(10))
        ScheduleMaskFactory(
            schedule=group,
            target_date=MONDAY,
            type=MaskType.RESCHEDULE,
            new_start_time=datetime.time(15),
            new_end_time=datetime.time(16),
        )

        result = list_trial_slots(activity.pk, now=_at(MONDAY, 12))

        assert [(slot.date, slot.start_time) for slot in result.slots] == [
            (MONDAY, datetime.time(15)),
            (MONDAY + WEEK, datetime.time(10)),
        ]

    def test_inactive_group_and_other_activity_are_not_listed(
        self, activity: Activity
    ) -> None:
        active = _group(activity, 2, datetime.time(17))
        _group(activity, 4, datetime.time(17), is_active=False)
        _group(ActivityFactory(), 5, datetime.time(10))

        result = list_trial_slots(activity.pk, now=_at(MONDAY, 9))

        assert {slot.schedule_id for slot in result.slots} == {active.pk}

    def test_queries_do_not_grow_with_groups(
        self,
        activity: Activity,
        django_assert_num_queries: DjangoAssertNumQueries,
    ) -> None:
        for day in range(7):
            _group(activity, day, datetime.time(17))
            _group(activity, day, datetime.time(9))
        # Маска в окне — чтобы запрос масок реально возвращал строки
        friday = Schedule.objects.get(
            activity=activity, day_of_week=4, start_time=datetime.time(17)
        )
        ScheduleMaskFactory(schedule=friday, target_date=FRIDAY)

        # Среда: окно задевает 3 недели, по 2 запроса на неделю (группы + маски)
        with django_assert_num_queries(6):
            result = list_trial_slots(activity.pk, now=_at(WEDNESDAY, 9))

        assert len(result.slots) > 20


class TestIsLessonBookable:
    def test_boundary_is_lesson_start(self) -> None:
        start = datetime.time(16)
        assert is_lesson_bookable(MONDAY, start, now=_at(MONDAY, 15, 59))
        assert not is_lesson_bookable(MONDAY, start, now=_at(MONDAY, 16))
        assert not is_lesson_bookable(MONDAY, start, now=_at(MONDAY, 17))


class TestNextSlotsEndpoint:
    def test_response_shape(self, activity: Activity) -> None:
        group = _group(activity, 2, datetime.time(17), group_name="Младшая")

        response = APIClient().get(_slots_url(activity.pk))

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["activity"] == {"id": activity.pk, "name": "Шахматы"}
        today = timezone.localdate()
        assert body["date_from"] == today.isoformat()
        assert body["date_to"] == (today + datetime.timedelta(days=13)).isoformat()
        first = body["slots"][0]
        assert set(first) == {
            "schedule_id",
            "date",
            "start_time",
            "end_time",
            "group_name",
            "teacher",
            "is_rescheduled",
        }
        assert first["schedule_id"] == group.pk
        assert (first["start_time"], first["end_time"]) == ("17:00", "18:00")
        assert first["group_name"] == "Младшая"
        assert set(first["teacher"]) == {"id", "full_name"}
        assert first["is_rescheduled"] is False

    def test_no_free_lessons_is_empty_list(self, activity: Activity) -> None:
        group = _group(activity, 2, datetime.time(17), max_capacity=1)
        EnrollmentFactory(schedule=group)

        response = APIClient().get(_slots_url(activity.pk))

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["slots"] == []

    def test_inactive_activity_is_404(self) -> None:
        closed = ActivityFactory(is_active=False)
        _group(closed, 2, datetime.time(17))

        response = APIClient().get(_slots_url(closed.pk))

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["code"] == "NOT_FOUND"

    def test_unknown_activity_is_404(self) -> None:
        response = APIClient().get(_slots_url(999_999))

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_queries_do_not_grow_with_groups(self, activity: Activity) -> None:
        _group(activity, 2, datetime.time(17))
        with CaptureQueriesContext(connection) as one_group:
            APIClient().get(_slots_url(activity.pk))

        other: Activity = ActivityFactory()
        for day in range(7):
            _group(other, day, datetime.time(17))
            _group(other, day, datetime.time(9))
        with CaptureQueriesContext(connection) as many_groups:
            APIClient().get(_slots_url(other.pk))

        assert len(many_groups) == len(one_group) <= 7


class TestSlotsPassTrialCheckout:
    def test_every_slot_is_accepted_by_checkout_date_check(
        self, activity: Activity
    ) -> None:
        for day in range(7):
            _group(activity, day, datetime.time(23), datetime.time(23, 45))
        # Перенос на другой день и время на следующей неделе
        moved = _group(activity, 2, datetime.time(17))
        ScheduleMaskFactory(
            schedule=moved,
            target_date=WEDNESDAY,
            type=MaskType.RESCHEDULE,
            new_day_of_week=4,
            new_start_time=datetime.time(18),
            new_end_time=datetime.time(19),
        )
        port = DjangoSchedulePort()

        slots = APIClient().get(_slots_url(activity.pk)).json()["slots"]

        assert any(slot["is_rescheduled"] for slot in slots)
        for slot in slots:
            trial_date = datetime.date.fromisoformat(slot["date"])
            assert (
                port.get_next_lesson_date(slot["schedule_id"], trial_date) == trial_date
            )

    def test_first_slot_can_be_bought(self, activity: Activity) -> None:
        _group(activity, 2, datetime.time(17))
        _group(activity, 5, datetime.time(10))
        parent = ParentFactory()
        student = StudentFactory(parent=parent)

        first = APIClient().get(_slots_url(activity.pk)).json()["slots"][0]
        result = create_trial_payment(
            parent_id=parent.pk,
            student_id=student.pk,
            schedule_id=first["schedule_id"],
            trial_date=datetime.date.fromisoformat(first["date"]),
            idempotency_key=str(uuid.uuid4()),
            request_fingerprint="trial-slots",
            gateway=FakeGateway(),
            schedule_port=DjangoSchedulePort(),
        )

        assert result.status == "PENDING_PAYMENT"
