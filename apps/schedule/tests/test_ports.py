from __future__ import annotations

import datetime

import pytest
from django.utils import timezone

from apps.billing.ports import (
    SlotTrialInfo,
    UnknownSlotError,
    resolve_schedule_port,
)
from apps.schedule.models import MaskType, Schedule
from apps.schedule.ports import DjangoSchedulePort
from apps.schedule.tests.factories import (
    ActivityFactory,
    ScheduleFactory,
    ScheduleMaskFactory,
    TimeSlotFactory,
)

# Понедельник; все даты в тестах отсчитываются от него, чтобы не зависеть от now()
MONDAY = datetime.date(2026, 8, 3)
WEDNESDAY_INDEX = 2
WEDNESDAY = MONDAY + datetime.timedelta(days=WEDNESDAY_INDEX)
WEEK = datetime.timedelta(weeks=1)


def _at(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    return timezone.make_aware(
        datetime.datetime.combine(day, datetime.time(hour, minute))
    )


def _wednesday_schedule(**kwargs: object) -> Schedule:
    return ScheduleFactory(
        time_slot=TimeSlotFactory(
            day_of_week=WEDNESDAY_INDEX,
            start_time=datetime.time(16, 0),
            end_time=datetime.time(17, 0),
        ),
        **kwargs,
    )


@pytest.mark.django_db
class TestResolveDefaultPort:
    def test_default_settings_resolve_django_port(self) -> None:
        # ПОЧЕМУ: фиксируем контракт BILLING_SCHEDULE_PORT_CLASS по умолчанию —
        # ровно этот шов был сломан (класс не существовал, тесты мокали порт)
        port = resolve_schedule_port()
        assert isinstance(port, DjangoSchedulePort)


@pytest.mark.django_db
class TestGetSlotCapacity:
    def test_returns_max_capacity_from_db(self) -> None:
        schedule = _wednesday_schedule(max_capacity=9)
        assert DjangoSchedulePort().get_slot_capacity(schedule.pk) == 9

    def test_unknown_slot_raises(self) -> None:
        with pytest.raises(UnknownSlotError):
            DjangoSchedulePort().get_slot_capacity(999_999)

    def test_inactive_schedule_is_not_sellable(self) -> None:
        schedule = _wednesday_schedule(is_active=False)
        with pytest.raises(UnknownSlotError):
            DjangoSchedulePort().get_slot_capacity(schedule.pk)

    def test_inactive_activity_is_not_sellable(self) -> None:
        schedule = _wednesday_schedule(activity=ActivityFactory(is_active=False))
        with pytest.raises(UnknownSlotError):
            DjangoSchedulePort().get_slot_capacity(schedule.pk)


@pytest.mark.django_db
class TestGetSlotTrialInfo:
    def test_returns_activity_and_price(self) -> None:
        activity = ActivityFactory(price=150_000)
        schedule = _wednesday_schedule(activity=activity)

        info = DjangoSchedulePort().get_slot_trial_info(schedule.pk)

        assert info == SlotTrialInfo(activity_id=activity.pk, price_kopecks=150_000)

    def test_unknown_slot_raises(self) -> None:
        with pytest.raises(UnknownSlotError):
            DjangoSchedulePort().get_slot_trial_info(999_999)

    def test_inactive_activity_is_not_sellable(self) -> None:
        schedule = _wednesday_schedule(activity=ActivityFactory(is_active=False))
        with pytest.raises(UnknownSlotError):
            DjangoSchedulePort().get_slot_trial_info(schedule.pk)


@pytest.mark.django_db
class TestGetNextLessonDate:
    def test_plain_grid_next_weekday(self) -> None:
        schedule = _wednesday_schedule()
        date = DjangoSchedulePort().get_next_lesson_date(schedule.pk, _at(MONDAY, 9))
        assert date == WEDNESDAY

    def test_same_day_before_start_counts(self) -> None:
        schedule = _wednesday_schedule()
        date = DjangoSchedulePort().get_next_lesson_date(
            schedule.pk, _at(WEDNESDAY, 15, 59)
        )
        assert date == WEDNESDAY

    def test_same_day_from_start_falls_to_next_week(self) -> None:
        # Граница — начало занятия (BOOKING_CUTOFF = 0)
        schedule = _wednesday_schedule()
        port = DjangoSchedulePort()
        for hour in (16, 19):
            date = port.get_next_lesson_date(schedule.pk, _at(WEDNESDAY, hour))
            assert date == WEDNESDAY + WEEK

    def test_utc_moment_uses_local_day(self) -> None:
        # 22:30 UTC вторника — уже 05:30 среды по Новосибирску (UTC+7):
        # по дате UTC занятие среды выглядело бы «завтрашним», но оно сегодня
        schedule = _wednesday_schedule()
        tuesday_utc = datetime.datetime.combine(
            WEDNESDAY, datetime.time(0), tzinfo=datetime.UTC
        ) - datetime.timedelta(hours=1, minutes=30)
        assert timezone.localdate(tuesday_utc) == WEDNESDAY
        date = DjangoSchedulePort().get_next_lesson_date(schedule.pk, tuesday_utc)
        assert date == WEDNESDAY

    def test_cancellation_mask_skips_to_next_week(self) -> None:
        schedule = _wednesday_schedule()
        ScheduleMaskFactory(
            schedule=schedule,
            target_date=WEDNESDAY,
            type=MaskType.CANCELLATION,
        )
        date = DjangoSchedulePort().get_next_lesson_date(schedule.pk, _at(MONDAY, 9))
        assert date == WEDNESDAY + WEEK

    def test_reschedule_mask_moves_landing_day(self) -> None:
        schedule = _wednesday_schedule()
        ScheduleMaskFactory(
            schedule=schedule,
            target_date=WEDNESDAY,
            reschedule=True,
            new_day_of_week=4,  # пятница той же недели
        )
        date = DjangoSchedulePort().get_next_lesson_date(schedule.pk, _at(MONDAY, 9))
        assert date == MONDAY + datetime.timedelta(days=4)

    def test_reschedule_landing_before_window_falls_to_next_week(self) -> None:
        # Занятие среды перенесли на понедельник; для запроса «со вторника»
        # приземление уже в прошлом — ближайшее занятие через неделю
        schedule = _wednesday_schedule()
        ScheduleMaskFactory(
            schedule=schedule,
            target_date=WEDNESDAY,
            reschedule=True,
            new_day_of_week=0,
        )
        tuesday = MONDAY + datetime.timedelta(days=1)
        date = DjangoSchedulePort().get_next_lesson_date(schedule.pk, _at(tuesday, 9))
        assert date == WEDNESDAY + WEEK

    def test_reschedule_later_same_day_still_open(self) -> None:
        schedule = _wednesday_schedule()
        ScheduleMaskFactory(
            schedule=schedule,
            target_date=WEDNESDAY,
            type=MaskType.RESCHEDULE,
            new_start_time=datetime.time(20),
            new_end_time=datetime.time(21),
        )
        port = DjangoSchedulePort()
        assert port.get_next_lesson_date(schedule.pk, _at(WEDNESDAY, 19)) == WEDNESDAY

    def test_reschedule_earlier_same_day_already_closed(self) -> None:
        schedule = _wednesday_schedule()
        ScheduleMaskFactory(
            schedule=schedule,
            target_date=WEDNESDAY,
            type=MaskType.RESCHEDULE,
            new_start_time=datetime.time(10),
            new_end_time=datetime.time(11),
        )
        date = DjangoSchedulePort().get_next_lesson_date(
            schedule.pk, _at(WEDNESDAY, 12)
        )
        assert date == WEDNESDAY + WEEK

    def test_unknown_slot_raises(self) -> None:
        with pytest.raises(UnknownSlotError):
            DjangoSchedulePort().get_next_lesson_date(999_999, _at(MONDAY, 9))
