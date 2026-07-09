# ПОЧЕМУ: Проекция регулярной сетки на даты и наложение масок
# Вместимость считается SQL-агрегацией, домен billing не импортируется для чистой изоляции
# Бюджет: 2 SQL-запроса на сетку недели

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.schedule.models import DayOfWeek, MaskType, Room, Schedule, ScheduleMask

if TYPE_CHECKING:
    from apps.users.models import TeacherProfile

RESCHEDULE_REASON = "Перенос"


@dataclass(frozen=True, slots=True)
class SlotOverride:
    # ПОЧЕМУ: DTO для контракта ответа. \
    # Фронтенду необходимо знать оригинальное время до переноса для UI

    original_start_time: datetime.time
    reason: str


@dataclass(frozen=True, slots=True)
class WeekSlot:
    # ПОЧЕМУ: Финальный DTO слота сетки.
    # Формируется в памяти после применения масок для передачи в тонкий сериализатор

    schedule_id: int
    date: datetime.date
    day_of_week: int
    start_time: datetime.time
    end_time: datetime.time
    activity_id: int
    activity_name: str
    activity_slug: str
    teacher_id: int | None
    teacher_full_name: str | None
    room_id: int | None
    room_name: str | None
    capacity_max: int
    capacity_taken: int
    capacity_free: int
    is_rescheduled: bool
    is_cancelled: bool
    override: SlotOverride | None


_MaskIndex: TypeAlias = Mapping[tuple[int, datetime.date], ScheduleMask]


def normalize_week_start(
    day: datetime.date,
) -> datetime.date:  # ПОЧЕМУ: Вместимость считается SQL-агрегацией, домен billing не импортируется для чистой изоляции.
    # Бюджет: 2 SQL-запроса на сетку недели
    return day - datetime.timedelta(days=day.weekday())


# ПОЧЕМУ: Фиксированный порядок захвата (кабинет -> преподаватель)
# исключает deadlock между параллельными переносами
_LOCK_NS_ROOM = 0x524F4F4D  # "ROOM"
_LOCK_NS_TEACHER = 0x54454348  # "TECH"


@dataclass(frozen=True, slots=True)
class _EffectiveSession:
    # ПОЧЕМУ: Промежуточная проекция приземления группы.
    # Изолирует логику наследования new_* для проверки коллизий до записи в БД

    date: datetime.date
    day_of_week: int
    start_time: datetime.time
    end_time: datetime.time
    room_id: int | None
    teacher_id: int | None


def create_schedule_mask(
    *,
    schedule: Schedule,
    target_date: datetime.date,
    mask_type: MaskType,
    new_day_of_week: int | None = None,
    new_start_time: datetime.time | None = None,
    new_end_time: datetime.time | None = None,
    new_room: Room | None = None,
    new_teacher: TeacherProfile | None = None,
) -> ScheduleMask:
    # ПОЧЕМУ: Блокировка строки через SELECT FOR UPDATE сериализует конкурентные маски одной группы
    # и гарантирует актуальность денормализованных триггером полей
    # ПОЧЕМУ: Advisory-локи сериализуют переносы разных групп на один ресурс
    # Ретро-маски запрещены для сохранения истории посещаемости
    # IntegrityError при гонке уникальности маппится в ValidationError
    if schedule.pk is None:
        raise ValidationError({"schedule": "Группа не сохранена в БД."}, code="invalid")
    if target_date < timezone.localdate():
        raise ValidationError(
            {"target_date": "Маска на прошедшую дату запрещена."}, code="invalid"
        )
    try:
        with transaction.atomic():
            locked = Schedule.objects.select_for_update().get(pk=schedule.pk)
            _validate_mask_target(locked, target_date)
            mask = ScheduleMask(
                schedule=locked,
                target_date=target_date,
                type=mask_type,
                new_day_of_week=new_day_of_week,
                new_start_time=new_start_time,
                new_end_time=new_end_time,
                new_room=new_room,
                new_teacher=new_teacher,
            )
            mask.full_clean()
            if mask_type == MaskType.RESCHEDULE:
                landing = _effective_session(locked, mask)
                _lock_resources(landing)
                _ensure_no_collision(landing, exclude_schedule_id=locked.pk)
            mask.save()
    except IntegrityError as exc:
        # Единственный INSERT в транзакции — сама маска; FK-существование уже
        # проверено full_clean, реалистичный источник — проигранная гонка на
        # uniq_mask_per_schedule_per_date.
        raise ValidationError(
            {"target_date": "Маска для этой группы на эту дату уже существует."},
            code="conflict",
        ) from exc
    return mask


def _validate_mask_target(schedule: Schedule, target_date: datetime.date) -> None:
    # ПОЧЕМУ: Проверка денормализованного поля свежепрочитанной строки без JOIN и N+1.
    # Межсущностные инварианты недоступны в ScheduleMask.clean()
    if not schedule.is_active:
        raise ValidationError(
            {"schedule": "Маска на неактивную группу не имеет смысла."},
            code="invalid",
        )
    # Денормализованное поле свежепрочитанной строки — без JOIN и без N+1.
    if target_date.weekday() != schedule.day_of_week:
        raise ValidationError(
            {
                "target_date": (
                    f"Дата {target_date.isoformat()} не попадает на день недели "
                    f"группы ({DayOfWeek(schedule.day_of_week).label}) — такая "
                    "маска никогда не применится."
                )
            },
            code="invalid",
        )


def _effective_session(schedule: Schedule, mask: ScheduleMask) -> _EffectiveSession:
    # ПОЧЕМУ: Расчет физического приземления переноса с наследованием пустых new_*.
    # Строка schedule должна быть прочитана из СУБД
    day_of_week = (
        mask.new_day_of_week
        if mask.new_day_of_week is not None
        else schedule.day_of_week
    )
    start_time = (
        mask.new_start_time if mask.new_start_time is not None else schedule.start_time
    )
    end_time = mask.new_end_time if mask.new_end_time is not None else schedule.end_time
    room_id = mask.new_room_id if mask.new_room_id is not None else schedule.room_id
    teacher_id = (
        mask.new_teacher_id if mask.new_teacher_id is not None else schedule.teacher_id
    )
    return _EffectiveSession(
        date=normalize_week_start(mask.target_date)
        + datetime.timedelta(days=day_of_week),
        day_of_week=day_of_week,
        start_time=start_time,
        end_time=end_time,
        room_id=room_id,
        teacher_id=teacher_id,
    )


def _lock_resources(session: _EffectiveSession) -> None:
    # ПОЧЕМУ: Транзакционные advisory-локи предотвращают гонку параллельных переносов на один ресурс.
    # Снимаются на COMMIT/ROLLBACK автоматически
    with connection.cursor() as cursor:
        if session.room_id is not None:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                [_LOCK_NS_ROOM, session.room_id],
            )
        if session.teacher_id is not None:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                [_LOCK_NS_TEACHER, session.teacher_id],
            )


def _ensure_no_collision(
    session: _EffectiveSession, *, exclude_schedule_id: int
) -> None:
    # ПОЧЕМУ: Проверка доступности ресурсов на дату приземления против
    # регулярной сетки (за вычетом отмен) и чужих переносов
    conflicts = sorted(
        _grid_collision_fields(session, exclude_schedule_id=exclude_schedule_id)
        | _mask_collision_fields(session, exclude_schedule_id=exclude_schedule_id)
    )
    if not conflicts:
        return
    messages = {
        "new_room": "Кабинет занят в это время на эту дату.",
        "new_teacher": "Преподаватель занят в это время на эту дату.",
    }
    raise ValidationError(
        {field: messages[field] for field in conflicts}, code="conflict"
    )


def _grid_collision_fields(
    session: _EffectiveSession, *, exclude_schedule_id: int
) -> set[str]:
    # ПОЧЕМУ: Поиск коллизий с регулярной сеткой.
    # Группы, чьи занятия отменены маской на эту дату, ресурс освобождают
    resource_terms = []
    if session.room_id is not None:
        resource_terms.append(Q(room_id=session.room_id))
    if session.teacher_id is not None:
        resource_terms.append(Q(teacher_id=session.teacher_id))
    if not resource_terms:
        return set()
    resource = resource_terms[0]
    for term in resource_terms[1:]:
        resource |= term
    occupants = (
        Schedule.objects.filter(
            is_active=True,
            activity__is_active=True,
            day_of_week=session.day_of_week,
            start_time__lt=session.end_time,
            end_time__gt=session.start_time,
        )
        .filter(resource)
        .exclude(pk=exclude_schedule_id)
        # Группа с маской на эту дату регулярное занятие не проводит:
        # отмена освобождает ресурс, перенос учитывается приземлением
        # в _mask_collision_fields.
        .exclude(masks__target_date=session.date)
        .values_list("room_id", "teacher_id")
    )
    return _collided_fields(session, occupants)


def _mask_collision_fields(
    session: _EffectiveSession, *, exclude_schedule_id: int
) -> set[str]:
    # ПОЧЕМУ: Выборка ограничена 7 днями текущей недели,
    # так как перенос проецируется строго внутри недельного цикла от понедельника
    week_start = normalize_week_start(session.date)
    others = (
        ScheduleMask.objects.filter(
            type=MaskType.RESCHEDULE,
            target_date__range=(
                week_start,
                week_start + datetime.timedelta(days=6),
            ),
            schedule__is_active=True,
            schedule__activity__is_active=True,
        )
        .exclude(schedule_id=exclude_schedule_id)
        .select_related("schedule")
    )
    landings = []
    for other in others:
        landing = _effective_session(other.schedule, other)
        if landing.date != session.date:
            continue
        if not (
            landing.start_time < session.end_time
            and session.start_time < landing.end_time
        ):
            continue
        landings.append((landing.room_id, landing.teacher_id))
    return _collided_fields(session, landings)


def _collided_fields(
    session: _EffectiveSession,
    occupants: Iterable[tuple[int | None, int | None]],
) -> set[str]:
    # ПОЧЕМУ: Маппинг занятых ресурсов на поля модели для точечной генерации ValidationError
    fields: set[str] = set()
    for room_id, teacher_id in occupants:
        if session.room_id is not None and room_id == session.room_id:
            fields.add("new_room")
        if session.teacher_id is not None and teacher_id == session.teacher_id:
            fields.add("new_teacher")
    return fields


def build_week_grid(week_start: datetime.date) -> list[WeekSlot]:
    # ПОЧЕМУ: Вся логика завязана на недельную сетку,
    # любая дата нормализуется к понедельнику
    week_start = normalize_week_start(week_start)
    week_end = week_start + datetime.timedelta(days=6)

    schedules = list(
        Schedule.objects.filter(is_active=True, activity__is_active=True)
        .select_related("activity", "teacher__user", "room")
        .annotate(
            capacity_taken=Count(
                "enrollment",
                filter=Q(enrollment__status__in=["HELD", "ENROLLED"]),
            )
        )
    )
    if not schedules:
        return []

    masks = _load_masks(
        schedule_ids=(schedule.pk for schedule in schedules),
        week_start=week_start,
        week_end=week_end,
    )
    slots = [
        _project_schedule(schedule, week_start=week_start, masks=masks)
        for schedule in schedules
    ]
    slots.sort(key=lambda slot: (slot.day_of_week, slot.start_time, slot.schedule_id))
    return slots


def _load_masks(
    *,
    schedule_ids: Iterable[int],
    week_start: datetime.date,
    week_end: datetime.date,
) -> _MaskIndex:
    # ПОЧЕМУ: Выборка всех масок недели одним запросом для исключения N+1.
    #  Ключ уникален по UniqueConstraint в БД
    masks = ScheduleMask.objects.filter(
        schedule_id__in=list(schedule_ids),
        target_date__range=(week_start, week_end),
    ).select_related("new_room", "new_teacher__user")
    return {(mask.schedule_id, mask.target_date): mask for mask in masks}


def _project_schedule(
    schedule: Schedule,
    *,
    week_start: datetime.date,
    masks: _MaskIndex,
) -> WeekSlot:
    # ПОЧЕМУ: Проекция группы на календарный день с учетом масок.
    #  Отмененное занятие остается в сетке с флагом для фронтенда
    session_date = week_start + datetime.timedelta(days=schedule.day_of_week)
    mask = masks.get((schedule.pk, session_date))
    if mask is None:
        return _build_slot(
            schedule,
            session_date=session_date,
            day_of_week=schedule.day_of_week,
            start_time=schedule.start_time,
            end_time=schedule.end_time,
            teacher=schedule.teacher,
            room=schedule.room,
            is_rescheduled=False,
            is_cancelled=False,
            override=None,
        )
    if mask.type == MaskType.CANCELLATION:
        # Отменённое занятие остаётся в сетке с флагом — фронт рисует его
        # перечёркнутым, слот не «исчезает» молча.
        return _build_slot(
            schedule,
            session_date=session_date,
            day_of_week=schedule.day_of_week,
            start_time=schedule.start_time,
            end_time=schedule.end_time,
            teacher=schedule.teacher,
            room=schedule.room,
            is_rescheduled=False,
            is_cancelled=True,
            override=None,
        )
    return _apply_reschedule(schedule, mask, week_start=week_start)


def _apply_reschedule(
    schedule: Schedule,
    mask: ScheduleMask,
    *,
    week_start: datetime.date,
) -> WeekSlot:
    # ПОЧЕМУ: При частичном переносе пустые поля new_* фолбэчатся на значения оригинальной группы
    day_of_week = (
        mask.new_day_of_week
        if mask.new_day_of_week is not None
        else schedule.day_of_week
    )
    start_time = (
        mask.new_start_time if mask.new_start_time is not None else schedule.start_time
    )
    end_time = mask.new_end_time if mask.new_end_time is not None else schedule.end_time
    teacher = mask.new_teacher if mask.new_teacher_id is not None else schedule.teacher
    room = mask.new_room if mask.new_room_id is not None else schedule.room
    return _build_slot(
        schedule,
        session_date=week_start + datetime.timedelta(days=day_of_week),
        day_of_week=day_of_week,
        start_time=start_time,
        end_time=end_time,
        teacher=teacher,
        room=room,
        is_rescheduled=True,
        is_cancelled=False,
        override=SlotOverride(
            original_start_time=schedule.start_time,
            reason=RESCHEDULE_REASON,
        ),
    )


def _build_slot(
    schedule: Schedule,
    *,
    session_date: datetime.date,
    day_of_week: int,
    start_time: datetime.time,
    end_time: datetime.time,
    teacher: TeacherProfile | None,
    room: Room | None,
    is_rescheduled: bool,
    is_cancelled: bool,
    override: SlotOverride | None,
) -> WeekSlot:
    activity = schedule.activity
    capacity_taken = schedule.capacity_taken
    return WeekSlot(
        schedule_id=schedule.pk,
        date=session_date,
        day_of_week=day_of_week,
        start_time=start_time,
        end_time=end_time,
        activity_id=activity.pk,
        activity_name=activity.name,
        activity_slug=activity.slug,
        teacher_id=teacher.pk if teacher is not None else None,
        teacher_full_name=_teacher_full_name(teacher),
        room_id=room.pk if room is not None else None,
        room_name=room.name if room is not None else None,
        capacity_max=schedule.max_capacity,
        capacity_taken=capacity_taken,
        capacity_free=max(schedule.max_capacity - capacity_taken, 0),
        is_rescheduled=is_rescheduled,
        is_cancelled=is_cancelled,
        override=override,
    )


def _teacher_full_name(teacher: TeacherProfile | None) -> str | None:
    if teacher is None:
        return None
    parts = (teacher.user.full_name, teacher.middle_name)
    full_name = " ".join(part for part in parts if part)
    return full_name or None
