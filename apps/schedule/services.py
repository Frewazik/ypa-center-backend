# ПОЧЕМУ: Проекция регулярной сетки на даты и наложение масок
# Вместимость считается SQL-агрегацией, домен billing не импортируется для чистой изоляции
# Бюджет: 2 SQL-запроса на сетку недели

from __future__ import annotations

import datetime
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, TypeAlias

from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.db import IntegrityError, connection, models, transaction
from django.db.models import Count, F, Func, Q, Value
from django.utils import timezone

from apps.billing.selectors import lesson_seat_holders, regular_seat_q, trial_seat_q
from apps.core.locks import advisory_xact_lock, advisory_xact_lock_many
from apps.core.queue import kiq_safely
from apps.schedule.models import DayOfWeek, MaskType, Room, Schedule, ScheduleMask

if TYPE_CHECKING:
    from apps.users.models import TeacherProfile

RESCHEDULE_REASON = "Перенос"


class WeekSessionDate(Func):
    # ПОЧЕМУ: в PostgreSQL date + integer = сдвиг в днях, поэтому дата занятия
    # строки сетки выражается скаляром `week_start + day_of_week`. Это снимает
    # нужду в оконной функции: у строки недельной сетки ровно одна дата, и
    # пробное сравнивается с ней равенством, а не агрегируется по дням.
    # week_start уходит биндом, а не в текст запроса

    arg_joiner = " + "
    template = "(%(expressions)s)"
    output_field = models.DateField()

    def __init__(self, week_start: datetime.date) -> None:
        super().__init__(
            Value(week_start, output_field=models.DateField()),
            F("day_of_week"),
        )


@dataclass(frozen=True, slots=True)
class SlotOverride:
    # ПОЧЕМУ: DTO для контракта ответа
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
    group_name: str
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
) -> datetime.date:
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
    if schedule.pk is None:
        raise ValidationError({"schedule": "Группа не сохранена в БД."}, code="invalid")
    if target_date < timezone.localdate():
        # ПОЧЕМУ: Ретро-маски запрещены, иначе задним числом переписывается история
        # посещаемости, которая уже могла отметиться по факту занятия
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
            _notify_families(locked.pk, target_date, change="created")
    except IntegrityError as exc:
        # Единственный INSERT в транзакции сама маска; FK-существование уже
        # проверено full_clean, реалистичный источник, проигранная гонка на
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
    # ПОЧЕМУ: пустые new_* в частичном переносе наследуют исходное значение
    # группы — маска может двигать только время, оставляя день, например
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
    # ПОЧЕМУ: транзакционные advisory-локи предотвращают гонку
    # параллельных переносов на один ресурс
    if session.room_id is not None:
        advisory_xact_lock(_LOCK_NS_ROOM, session.room_id)
    if session.teacher_id is not None:
        advisory_xact_lock(_LOCK_NS_TEACHER, session.teacher_id)


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


LessonChange: TypeAlias = Literal["created", "removed"]


def delete_schedule_mask(*, mask_id: int) -> None:
    # ПОЧЕМУ только будущие даты: на сегодня утренний таск журнала уже открыл
    # (или не открыл) занятие по маске — «отмена отмены» задним числом
    # разошлась бы с отметками посещаемости
    with transaction.atomic():
        mask = ScheduleMask.objects.select_for_update().filter(pk=mask_id).first()
        if mask is None:
            raise ValidationError("Перенос или отмена уже удалены.", code="not_found")
        if mask.target_date <= timezone.localdate():
            raise ValidationError(
                "Удалить можно только перенос или отмену на дату после сегодняшней.",
                code="invalid",
            )
        schedule_id, target_date = mask.schedule_id, mask.target_date
        mask.delete()
        _notify_families(schedule_id, target_date, change="removed")


def _notify_families(
    schedule_id: int, target_date: datetime.date, *, change: LessonChange
) -> None:
    # ПОЧЕМУ: получатели читаются в той же транзакции, что и правка маски, а
    # письма ставятся после коммита — по одной задаче на семью, чтобы ретрай
    # сбойного письма не повторял остальные
    parent_ids = sorted(
        set(
            lesson_seat_holders([schedule_id], on_date=target_date).values_list(
                "student__parent_id", flat=True
            )
        )
    )
    if not parent_ids:
        return

    def enqueue() -> None:
        # ПОЧЕМУ: локальный импорт — tasks импортирует services на уровне модуля
        from apps.schedule import tasks

        for parent_id in parent_ids:
            kiq_safely(
                tasks.send_lesson_change_email_task,
                parent_id,
                schedule_id,
                target_date.isoformat(),
                change,
            )

    transaction.on_commit(enqueue)


@dataclass(frozen=True, slots=True)
class FamilyContact:
    # ПОЧЕМУ: администратор обзванивает семьи сам (решение бизнеса), поэтому
    # в админке нужен готовый список с контактами, а не id записей
    group: str
    child_name: str
    parent_name: str
    phone: str
    email: str
    trial_date: datetime.date | None


def families_to_warn(
    schedule_ids: Iterable[int], *, on_date: datetime.date | None
) -> list[FamilyContact]:
    return [
        FamilyContact(
            group=str(enrollment.schedule),
            child_name=enrollment.student.full_name,
            parent_name=enrollment.student.parent.full_name,
            phone=str(enrollment.student.parent.phone or ""),
            email=enrollment.student.parent.email,
            trial_date=enrollment.trial_date,
        )
        for enrollment in lesson_seat_holders(schedule_ids, on_date=on_date)
    ]


@dataclass(frozen=True, slots=True)
class GridPlacement:
    # ПОЧЕМУ: место активной группы в постоянной сетке таким, каким его увидят
    # exclusion-ограничения БД после сохранения формы. Django их в форме не
    # проверяет (день и время группы — editable=False копии из слота), и
    # без этой проверки админка отвечала 500 на IntegrityError
    label: str
    day_of_week: int
    start_time: datetime.time
    end_time: datetime.time
    teacher_id: int | None
    room_id: int | None


def validate_grid_change(
    placements: Sequence[GridPlacement],
    *,
    moving_ids: Collection[int],
    day_changed: bool,
) -> None:
    # ПОЧЕМУ под локами: админка выполняет запрос в одной транзакции, и
    # «проверил → сохранил» атомарно только если локи держатся до коммита.
    # Порядок общий с create_schedule_mask: строка группы → кабинеты →
    # преподаватели, иначе встречные правки ловят взаимную блокировку
    if not connection.in_atomic_block:
        raise RuntimeError("validate_grid_change вызывается внутри transaction.atomic")
    list(
        Schedule.objects.select_for_update()
        .filter(pk__in=moving_ids)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    advisory_xact_lock_many(
        _LOCK_NS_ROOM, sorted({p.room_id for p in placements if p.room_id is not None})
    )
    advisory_xact_lock_many(
        _LOCK_NS_TEACHER,
        sorted({p.teacher_id for p in placements if p.teacher_id is not None}),
    )

    errors: dict[str, list[str]] = {}
    if day_changed:
        errors.update(_future_mask_errors(moving_ids))
    for placement in placements:
        for field, message in _grid_conflicts(placement, moving_ids=moving_ids):
            errors.setdefault(field, []).append(message)
    if errors:
        raise ValidationError(errors, code="conflict")


def _future_mask_errors(schedule_ids: Collection[int]) -> dict[str, list[str]]:
    # ПОЧЕМУ: маска ищется по паре (группа, дата), а дата занятия считается
    # от дня группы. После смены дня маска на старую дату молча перестаёт
    # применяться — отменённое занятие снова продаётся и открывается в журнале
    masks = (
        ScheduleMask.objects.filter(
            schedule_id__in=schedule_ids, target_date__gte=timezone.localdate()
        )
        .select_related("schedule")
        .order_by("target_date", "pk")
    )
    messages = [
        f"У группы «{mask.schedule}» есть {mask.get_type_display().lower()} на "
        f"{mask.target_date:%d.%m.%Y} — после смены дня она перестанет "
        "действовать. Сначала удалите её в «Расписание → Переносы и отмены»."
        for mask in masks
    ]
    return {NON_FIELD_ERRORS: messages} if messages else {}


def _grid_conflicts(
    placement: GridPlacement, *, moving_ids: Collection[int]
) -> Iterator[tuple[str, str]]:
    # ПОЧЕМУ: то же условие, что у no_teacher_time_overlap/no_room_time_overlap:
    # активные группы, без учёта активности кружка и масок — ограничение БД
    # про них не знает. Проверка на дату с масками — _ensure_no_collision
    resource = Q(pk__in=[])
    if placement.teacher_id is not None:
        resource |= Q(teacher_id=placement.teacher_id)
    if placement.room_id is not None:
        resource |= Q(room_id=placement.room_id)
    occupants = (
        Schedule.objects.filter(
            resource,
            is_active=True,
            day_of_week=placement.day_of_week,
            start_time__lt=placement.end_time,
            end_time__gt=placement.start_time,
        )
        .exclude(pk__in=moving_ids)
        .select_related("activity")
        .order_by("pk")
    )
    prefix = f"«{placement.label}»: " if placement.label else ""
    for occupant in occupants:
        busy = f"«{occupant}» ({occupant.activity.name})"
        if (
            placement.teacher_id is not None
            and occupant.teacher_id == placement.teacher_id
        ):
            yield "teacher", f"{prefix}Преподаватель занят в это время — группа {busy}."
        if placement.room_id is not None and occupant.room_id == placement.room_id:
            yield "room", f"{prefix}Кабинет занят в это время — группа {busy}."


def build_week_grid(
    week_start: datetime.date, *, activity_id: int | None = None
) -> list[WeekSlot]:
    # ПОЧЕМУ: Вся логика завязана на недельную сетку,
    # любая дата нормализуется к понедельнику.
    # activity_id сужает сетку до одного кружка (запись на пробное) —
    # маски и места считаются тем же кодом, без второй копии правил
    week_start = normalize_week_start(week_start)
    week_end = week_start + datetime.timedelta(days=6)

    groups = Schedule.objects.filter(is_active=True, activity__is_active=True)
    if activity_id is not None:
        groups = groups.filter(activity_id=activity_id)
    schedules = list(
        groups.select_related("activity", "teacher__user", "room")
        # FIXME: capacity_taken считает по базовой сетке, игнорируя даты
        # приземления RESCHEDULE-масок
        # ПОЧЕМУ ignore: capacity_taken объявлен на модели ради типизации
        # потребителей; django-stubs считает annotate() переопределением
        .annotate(  # type: ignore[no-redef]
            # Постоянные записи держат место всегда, пробные — только в свой
            # день. Оба COUNT(*) FILTER считаются одним проходом по тому же
            # JOIN: ни второго запроса, ни сортировки, ни партиционирования
            capacity_taken=Count("enrollment", filter=regular_seat_q("enrollment__"))
            + Count(
                "enrollment",
                filter=trial_seat_q(
                    "enrollment__", on_date=WeekSessionDate(week_start)
                ),
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


# ПОЧЕМУ: «две недели вперёд» — скользящее окно «сегодня + 13 дней», а не
# календарные недели: каждый день недели попадает в него ровно дважды,
# и в воскресенье родитель видит те же две недели, что и в понедельник
TRIAL_WINDOW_DAYS: Final[int] = 14

# ПОЧЕМУ: граница «ещё можно записаться» — за сколько до начала занятия
# закрывается запись. Решение бизнеса (2026-09-27): до самого начала.
# Задача 06 (lesson-time-cutoff) переиспользует is_lesson_bookable в чекауте
BOOKING_CUTOFF: Final[datetime.timedelta] = datetime.timedelta(0)


@dataclass(frozen=True, slots=True)
class TrialSlots:
    date_from: datetime.date
    date_to: datetime.date
    slots: list[WeekSlot]


def lesson_starts_at(
    session_date: datetime.date, start_time: datetime.time
) -> datetime.datetime:
    # ПОЧЕМУ: время групп хранится местным (Новосибирск) без пояса;
    # make_aware берёт текущий пояс проекта, сравнение с now() идёт в UTC
    return timezone.make_aware(datetime.datetime.combine(session_date, start_time))


def is_lesson_bookable(
    session_date: datetime.date,
    start_time: datetime.time,
    *,
    now: datetime.datetime,
) -> bool:
    # ПОЧЕМУ: на вход — фактические дата и время занятия (после маски
    # переноса), их отдаёт сетка; сама функция маски не применяет
    return now < lesson_starts_at(session_date, start_time) - BOOKING_CUTOFF


def list_trial_slots(activity_id: int, *, now: datetime.datetime) -> TrialSlots:
    # ПОЧЕМУ: окно режется из недельных сеток build_week_grid — единственного
    # места, где проецируются маски и считаются места на дату. Окно из 14 дней
    # задевает 2–3 недели с понедельника; бюджет — 2 запроса на неделю.
    # Известное ограничение — FIXME в build_week_grid: на перенесённом занятии
    # пробные считаются по исходной дате, чекаут перепроверит места сам
    date_from = timezone.localdate(now)
    date_to = date_from + datetime.timedelta(days=TRIAL_WINDOW_DAYS - 1)

    candidates: list[WeekSlot] = []
    week_start = normalize_week_start(date_from)
    while week_start <= date_to:
        candidates.extend(build_week_grid(week_start, activity_id=activity_id))
        week_start += datetime.timedelta(weeks=1)

    slots = [
        slot
        for slot in candidates
        if not slot.is_cancelled
        and slot.capacity_free > 0
        and date_from <= slot.date <= date_to
        and is_lesson_bookable(slot.date, slot.start_time, now=now)
    ]
    slots.sort(key=lambda slot: (slot.date, slot.start_time, slot.schedule_id))
    return TrialSlots(date_from=date_from, date_to=date_to, slots=slots)


def _load_masks(
    *,
    schedule_ids: Iterable[int],
    week_start: datetime.date,
    week_end: datetime.date,
) -> _MaskIndex:
    # ПОЧЕМУ: все маски недели — одним запросом, чтобы не бить N+1 по группам.
    # Ключ (schedule_id, target_date) уникален по констрейнту в БД
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
    # ПОЧЕМУ: отменённое занятие не выкидываем из сетки остаётся с флагом
    # is_cancelled, фронт рисует его перечёркнутым, а не молча прячет
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
        group_name=schedule.group_name,
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
