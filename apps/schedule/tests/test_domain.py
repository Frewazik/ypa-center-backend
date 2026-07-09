"""Тесты домена «Расписание»: проекция сетки, маски, вместимость, N+1, API."""

from __future__ import annotations

from typing import TYPE_CHECKING

from datetime import date, time, timedelta

import pytest
from django.core.exceptions import ValidationError
from pytest_django import DjangoAssertNumQueries
from rest_framework import status
from rest_framework.test import APIRequestFactory
from rest_framework.test import APIClient

from apps.schedule.models import MaskType, Room, Schedule, ScheduleMask, TimeSlot
from apps.schedule.services import (
    build_week_grid,
    create_schedule_mask,
    normalize_week_start,
)
from apps.schedule.tests.factories import (
    ActivityFactory,
    EnrollmentFactory,
    RoomFactory,
    ScheduleFactory,
    ScheduleMaskFactory,
    TeacherProfileFactory,
    TimeSlotFactory,
)
from apps.schedule.views import PublicScheduleView

if TYPE_CHECKING:
    from apps.users.models import TeacherProfile

pytestmark = pytest.mark.django_db

MONDAY = date(2026, 7, 6)  # понедельник
WEDNESDAY = MONDAY + timedelta(days=2)

# Сервис масок запрещает ретро-даты, поэтому его тесты живут на ближайшем
# БУДУЩЕМ понедельнике — иначе фиксированная дата протухнет и уронит CI.
FUTURE_MONDAY = date.today() + timedelta(days=7 - date.today().weekday())
FUTURE_WEDNESDAY = FUTURE_MONDAY + timedelta(days=2)


def _schedule_on(
    day_of_week: int,
    start: time,
    end: time,
    **kwargs: object,
) -> Schedule:
    time_slot = TimeSlotFactory(day_of_week=day_of_week, start_time=start, end_time=end)
    schedule: Schedule = ScheduleFactory(time_slot=time_slot, **kwargs)
    return schedule


def test_build_week_grid_projects_active_schedules_onto_dates() -> None:
    monday_group = _schedule_on(0, time(16), time(17))
    wednesday_group = _schedule_on(2, time(10), time(11))

    grid = build_week_grid(MONDAY)

    assert [slot.schedule_id for slot in grid] == [
        monday_group.pk,
        wednesday_group.pk,
    ]
    monday_slot, wednesday_slot = grid
    assert monday_slot.date == MONDAY
    assert monday_slot.day_of_week == 0
    assert (monday_slot.start_time, monday_slot.end_time) == (time(16), time(17))
    assert monday_slot.activity_name == monday_group.activity.name
    assert monday_slot.teacher_full_name is not None
    assert monday_slot.is_cancelled is False
    assert monday_slot.is_rescheduled is False
    assert monday_slot.override is None
    assert wednesday_slot.date == WEDNESDAY


def test_cancellation_mask_flags_slot_without_removing_it() -> None:
    schedule = _schedule_on(0, time(16), time(17))
    ScheduleMaskFactory(
        schedule=schedule, target_date=MONDAY
    )  # тип по умолчанию — отмена

    grid = build_week_grid(MONDAY)

    assert len(grid) == 1
    slot = grid[0]
    assert slot.is_cancelled is True
    assert slot.is_rescheduled is False
    assert (slot.start_time, slot.end_time) == (time(16), time(17))
    assert slot.override is None


def test_mask_outside_requested_week_is_ignored() -> None:
    schedule = _schedule_on(0, time(16), time(17))
    ScheduleMaskFactory(schedule=schedule, target_date=MONDAY + timedelta(days=7))

    slot = build_week_grid(MONDAY)[0]

    assert slot.is_cancelled is False
    assert slot.is_rescheduled is False


def test_reschedule_mask_overrides_time_and_keeps_original_in_override() -> None:
    schedule = _schedule_on(0, time(16), time(17))
    new_room = RoomFactory(name="Кабинет для переноса")
    ScheduleMaskFactory(
        schedule=schedule,
        target_date=MONDAY,
        reschedule=True,
        new_start_time=time(18),
        new_end_time=time(19),
        new_room=new_room,
    )

    slot = build_week_grid(MONDAY)[0]

    assert slot.is_rescheduled is True
    assert slot.is_cancelled is False
    assert (slot.start_time, slot.end_time) == (time(18), time(19))
    assert slot.room_name == "Кабинет для переноса"
    assert slot.override is not None
    assert slot.override.original_start_time == time(16)
    assert slot.override.reason == "Перенос"


def test_reschedule_mask_moves_slot_to_another_day() -> None:
    schedule = _schedule_on(0, time(16), time(17))
    ScheduleMaskFactory(
        schedule=schedule,
        target_date=MONDAY,
        reschedule=True,
        new_day_of_week=2,
        new_start_time=time(16),
        new_end_time=time(17),
    )

    slot = build_week_grid(MONDAY)[0]

    assert slot.day_of_week == 2
    assert slot.date == WEDNESDAY


def test_capacity_taken_counts_only_active_enrollments() -> None:
    schedule = _schedule_on(0, time(16), time(17), max_capacity=6)
    EnrollmentFactory.create_batch(2, schedule=schedule, is_active=True)
    EnrollmentFactory(schedule=schedule, is_active=False)

    slot = build_week_grid(MONDAY)[0]

    assert slot.capacity_max == 6
    assert slot.capacity_taken == 2
    assert slot.capacity_free == 4


def test_inactive_schedule_and_inactive_activity_excluded_from_grid() -> None:
    _schedule_on(0, time(16), time(17), is_active=False)
    _schedule_on(1, time(10), time(11), activity=ActivityFactory(is_active=False))
    active = _schedule_on(2, time(12), time(13))

    grid = build_week_grid(MONDAY)

    assert [slot.schedule_id for slot in grid] == [active.pk]


def test_week_start_normalized_to_monday() -> None:
    assert normalize_week_start(WEDNESDAY) == MONDAY
    assert normalize_week_start(MONDAY) == MONDAY

    schedule = _schedule_on(0, time(16), time(17))

    grid = build_week_grid(WEDNESDAY)

    assert grid[0].schedule_id == schedule.pk
    assert grid[0].date == MONDAY  # сетка недели, содержащей среду


def test_empty_schedule_returns_early_with_single_query(
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    with django_assert_num_queries(1):
        assert build_week_grid(MONDAY) == []


@pytest.mark.parametrize("schedule_count", [3, 30])
def test_build_week_grid_runs_exactly_two_queries(
    schedule_count: int,
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    schedules = ScheduleFactory.create_batch(schedule_count)
    for schedule in schedules:
        EnrollmentFactory(schedule=schedule, is_active=True)
    ScheduleMaskFactory(
        schedule=schedules[0],
        target_date=MONDAY + timedelta(days=schedules[0].time_slot.day_of_week),
    )
    ScheduleMaskFactory(
        schedule=schedules[1],
        target_date=MONDAY + timedelta(days=schedules[1].time_slot.day_of_week),
        reschedule=True,
        new_teacher=TeacherProfileFactory(),
        new_room=RoomFactory(),
    )

    with django_assert_num_queries(2):
        grid = build_week_grid(MONDAY)

    assert len(grid) == schedule_count
    assert all(slot.capacity_taken == 1 for slot in grid)
    assert sum(1 for slot in grid if slot.is_cancelled) == 1
    assert sum(1 for slot in grid if slot.is_rescheduled) == 1


def test_public_schedule_endpoint_matches_contract_shape() -> None:
    _schedule_on(0, time(16), time(17))
    request = APIRequestFactory().get(
        "/api/v1/public/schedule",
        {"week_start": "2026-07-08"},  # среда
    )

    response = PublicScheduleView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    assert response.data["week_start"] == "2026-07-06"  # нормализована к понедельнику
    assert response.data["week_end"] == "2026-07-12"
    slot = response.data["slots"][0]
    assert set(slot) == {
        "schedule_id",
        "date",
        "day_of_week",
        "start_time",
        "end_time",
        "activity",
        "teacher",
        "room",
        "capacity",
        "is_rescheduled",
        "is_cancelled",
        "override",
    }
    assert slot["date"] == "2026-07-06"
    assert slot["start_time"] == "16:00"
    assert set(slot["activity"]) == {"id", "name", "slug"}
    assert set(slot["teacher"]) == {"id", "full_name"}
    assert slot["capacity"] == {"max": 6, "taken": 0, "free": 6}
    assert slot["override"] is None


def test_public_schedule_rejects_malformed_week_start() -> None:
    # ПОЧЕМУ: APIRequestFactory изолирует запрос от конвейера DRF, ломая контекст
    # для RFC 9457. APIClient запускает полный цикл и гарантирует маппинг в 422.
    client = APIClient()

    response = client.get(
        "/api/v1/public/schedule/", {"week_start": "08.07.2026"}, format="json"
    )
    # 422 VALIDATION_ERROR по контракту §0.3: маппинг DRF ValidationError → 422
    # выполняет глобальный обработчик RFC 9457 в apps.core.exceptions.
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# --- Триггеры синхронизации денормализованных колонок (0003) ----------------
# save() у Schedule удалён осознанно: гарантия целостности живёт в БД и обязана
# переживать bulk-операции — именно их и проверяем.


def test_trigger_fills_denormalized_columns_on_bulk_create() -> None:
    time_slot = TimeSlotFactory(day_of_week=3, start_time=time(9), end_time=time(10))
    schedule = ScheduleFactory.build(
        activity=ActivityFactory(),
        time_slot=time_slot,
        teacher=TeacherProfileFactory(),
        room=RoomFactory(),
    )

    Schedule.objects.bulk_create([schedule])

    stored = Schedule.objects.get(pk=schedule.pk)
    assert stored.day_of_week == 3
    assert (stored.start_time, stored.end_time) == (time(9), time(10))


def test_trigger_resyncs_columns_on_queryset_update_of_time_slot() -> None:
    schedule = _schedule_on(0, time(16), time(17))
    other_slot = TimeSlotFactory(day_of_week=4, start_time=time(11), end_time=time(12))

    # QuerySet.update() идёт мимо Model.save() — ловит именно триггер.
    Schedule.objects.filter(pk=schedule.pk).update(time_slot=other_slot)

    schedule.refresh_from_db()
    assert schedule.day_of_week == 4
    assert (schedule.start_time, schedule.end_time) == (time(11), time(12))


def test_trigger_ignores_direct_writes_to_denormalized_columns() -> None:
    schedule = _schedule_on(0, time(16), time(17))

    Schedule.objects.filter(pk=schedule.pk).update(day_of_week=6)

    schedule.refresh_from_db()
    assert schedule.day_of_week == 0  # триггер перетёр мусор источником правды


def test_time_slot_change_propagates_to_schedules() -> None:
    schedule = _schedule_on(0, time(16), time(17))

    TimeSlot.objects.filter(pk=schedule.time_slot_id).update(
        day_of_week=2, start_time=time(8), end_time=time(9)
    )

    schedule.refresh_from_db()
    assert schedule.day_of_week == 2
    assert (schedule.start_time, schedule.end_time) == (time(8), time(9))


# --- Сервис create_schedule_mask ---------------------------------------------


def test_create_schedule_mask_creates_valid_cancellation() -> None:
    schedule = _schedule_on(0, time(16), time(17))

    mask = create_schedule_mask(
        schedule=schedule,
        target_date=FUTURE_MONDAY,
        mask_type=MaskType.CANCELLATION,
    )

    assert mask.pk is not None
    assert mask.is_cancelled is True
    assert build_week_grid(FUTURE_MONDAY)[0].is_cancelled is True


def test_create_schedule_mask_rejects_reschedule_without_times() -> None:
    schedule = _schedule_on(0, time(16), time(17))

    with pytest.raises(ValidationError) as excinfo:
        create_schedule_mask(
            schedule=schedule,
            target_date=FUTURE_MONDAY,
            mask_type=MaskType.RESCHEDULE,
        )

    assert {"new_start_time", "new_end_time"} <= set(excinfo.value.message_dict)


def test_create_schedule_mask_rejects_cancellation_with_overrides() -> None:
    schedule = _schedule_on(0, time(16), time(17))

    with pytest.raises(ValidationError) as excinfo:
        create_schedule_mask(
            schedule=schedule,
            target_date=FUTURE_MONDAY,
            mask_type=MaskType.CANCELLATION,
            new_start_time=time(18),
        )

    assert "new_start_time" in excinfo.value.message_dict


def test_create_schedule_mask_rejects_date_on_wrong_weekday() -> None:
    schedule = _schedule_on(0, time(16), time(17))  # группа по понедельникам

    with pytest.raises(ValidationError) as excinfo:
        create_schedule_mask(
            schedule=schedule,
            target_date=FUTURE_WEDNESDAY,
            mask_type=MaskType.CANCELLATION,
        )

    assert "target_date" in excinfo.value.message_dict


def test_create_schedule_mask_rejects_inactive_schedule() -> None:
    schedule = _schedule_on(0, time(16), time(17), is_active=False)

    with pytest.raises(ValidationError) as excinfo:
        create_schedule_mask(
            schedule=schedule,
            target_date=FUTURE_MONDAY,
            mask_type=MaskType.CANCELLATION,
        )

    assert "schedule" in excinfo.value.message_dict


def test_create_schedule_mask_rejects_past_target_date() -> None:
    last_monday = FUTURE_MONDAY - timedelta(days=14)
    schedule = _schedule_on(0, time(16), time(17))

    with pytest.raises(ValidationError) as excinfo:
        create_schedule_mask(
            schedule=schedule,
            target_date=last_monday,
            mask_type=MaskType.CANCELLATION,
        )

    assert "target_date" in excinfo.value.message_dict


def test_create_schedule_mask_rejects_duplicate_for_same_date() -> None:
    schedule = _schedule_on(0, time(16), time(17))
    create_schedule_mask(
        schedule=schedule,
        target_date=FUTURE_MONDAY,
        mask_type=MaskType.CANCELLATION,
    )

    with pytest.raises(ValidationError):
        create_schedule_mask(
            schedule=schedule,
            target_date=FUTURE_MONDAY,
            mask_type=MaskType.CANCELLATION,
        )


# --- Коллизии переносов: запрет двойного бронирования на дату ---------------


def _reschedule_into(
    schedule: Schedule,
    *,
    target_date: date | None = None,
    new_room: Room | None = None,
    new_teacher: TeacherProfile | None = None,
) -> ScheduleMask:
    """Перенос группы на понедельник 16:00–17:00 недели её ``target_date``."""
    return create_schedule_mask(
        schedule=schedule,
        target_date=(
            target_date
            if target_date is not None
            else FUTURE_MONDAY + timedelta(days=schedule.time_slot.day_of_week)
        ),
        mask_type=MaskType.RESCHEDULE,
        new_day_of_week=0,
        new_start_time=time(16),
        new_end_time=time(17),
        new_room=new_room,
        new_teacher=new_teacher,
    )


def test_reschedule_rejects_room_occupied_by_regular_schedule() -> None:
    room = RoomFactory()
    _schedule_on(0, time(16), time(17), room=room)  # понедельник, кабинет занят
    tuesday_group = _schedule_on(1, time(16), time(17))

    with pytest.raises(ValidationError) as excinfo:
        _reschedule_into(tuesday_group, new_room=room)

    assert "new_room" in excinfo.value.message_dict


def test_reschedule_rejects_busy_teacher() -> None:
    teacher = TeacherProfileFactory()
    _schedule_on(0, time(16), time(17), teacher=teacher)
    tuesday_group = _schedule_on(1, time(16), time(17))

    with pytest.raises(ValidationError) as excinfo:
        _reschedule_into(tuesday_group, new_teacher=teacher)

    assert "new_teacher" in excinfo.value.message_dict


def test_reschedule_allowed_into_room_freed_by_cancellation() -> None:
    room = RoomFactory()
    monday_group = _schedule_on(0, time(16), time(17), room=room)
    create_schedule_mask(
        schedule=monday_group,
        target_date=FUTURE_MONDAY,
        mask_type=MaskType.CANCELLATION,
    )
    tuesday_group = _schedule_on(1, time(16), time(17))

    mask = _reschedule_into(tuesday_group, new_room=room)

    assert mask.pk is not None


def test_reschedule_allowed_when_occupant_rescheduled_away() -> None:
    room = RoomFactory()
    monday_group = _schedule_on(0, time(16), time(17), room=room)
    create_schedule_mask(  # хозяин кабинета уехал на 18:00 — пересечения нет
        schedule=monday_group,
        target_date=FUTURE_MONDAY,
        mask_type=MaskType.RESCHEDULE,
        new_start_time=time(18),
        new_end_time=time(19),
    )
    tuesday_group = _schedule_on(1, time(16), time(17))

    mask = _reschedule_into(tuesday_group, new_room=room)

    assert mask.pk is not None


def test_reschedule_rejects_room_taken_by_another_masks_landing() -> None:
    room = RoomFactory()
    tuesday_group = _schedule_on(1, time(16), time(17))
    _reschedule_into(tuesday_group, new_room=room)  # первый перенос занял кабинет
    thursday_group = _schedule_on(3, time(16), time(17))

    with pytest.raises(ValidationError) as excinfo:
        _reschedule_into(thursday_group, new_room=room)

    assert "new_room" in excinfo.value.message_dict


def test_reschedule_checks_occupancy_on_landing_date_not_weekday() -> None:
    room = RoomFactory()
    # Регулярная сетка повторяется еженедельно: без отмены кабинет был бы занят
    # хозяином и на следующей неделе. Отмена освобождает ТОЛЬКО конкретную дату
    # приземления — именно на ней и проверяется занятость.
    monday_owner = _schedule_on(0, time(16), time(17), room=room)
    create_schedule_mask(
        schedule=monday_owner,
        target_date=FUTURE_MONDAY + timedelta(days=7),
        mask_type=MaskType.CANCELLATION,
    )
    next_week_tuesday_group = _schedule_on(1, time(16), time(17))

    mask = _reschedule_into(
        next_week_tuesday_group,
        target_date=FUTURE_MONDAY + timedelta(days=8),
        new_room=room,
    )

    assert mask.pk is not None
