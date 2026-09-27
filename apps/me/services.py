from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, TypeAlias, cast

from django.db import IntegrityError, transaction
from django.db.models import Case, IntegerField, Prefetch, Q, QuerySet, When
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from apps.billing.models import (
    DepositEntry,
    DepositEntryReason,
    Enrollment,
    EnrollmentStatus,
    EnrollmentType,
    ParentDeposit,
    Subscription,
    SubscriptionStatus,
    TransactionStatus,
)
from apps.events.models import SEAT_BLOCKING_STATUSES, EventRegistration
from apps.schedule.models import MaskType, ScheduleMask
from apps.users.models import Parent, Student

UPCOMING_DEFAULT_WEEKS: Final[int] = 4
UPCOMING_MAX_WEEKS: Final[int] = 8

_HISTORY_STATUSES: Final = (SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRED)

UpcomingKind: TypeAlias = Literal["SUBSCRIPTION_SESSION", "TRIAL", "EVENT"]


_WEEKDAY_ABBR_RU: Final = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")

# ПОЧЕМУ: метка в модели написана для админки; родителю «неисполненный заказ»
# непонятен. Остальные причины показываем меткой модели как есть
_DEPOSIT_REASON_DISPLAY_OVERRIDES: Final[dict[str, str]] = {
    DepositEntryReason.ORDER_CANCELED_RETURN: "Возврат: заказ не был оплачен",
}


@dataclass(frozen=True, slots=True)
class SubscriptionSlotView:
    schedule_id: int
    activity_name: str
    group_name: str
    schedule: str
    remaining_sessions: int
    total_sessions: int


@dataclass(frozen=True, slots=True)
class SubscriptionView:
    id: int
    display_id: str
    status: str
    student_name: str
    purchase_price: int
    created_at: datetime.datetime
    start_date: datetime.date | None
    expires_at: datetime.datetime | None
    total_remaining: int
    slots: list[SubscriptionSlotView]


@dataclass(frozen=True, slots=True)
class TrialView:
    id: int
    student_id: int
    student_name: str
    activity_name: str
    group_name: str
    trial_date: datetime.date
    start_time: datetime.time
    end_time: datetime.time
    status: str
    cost: int | None
    created_at: datetime.datetime


@dataclass(frozen=True, slots=True)
class DepositEntryView:
    id: int
    amount: int
    reason: str
    reason_display: str
    subscription_id: int | None
    subscription_display_id: str | None
    created_at: datetime.datetime


@dataclass(frozen=True, slots=True)
class UpcomingItem:
    kind: UpcomingKind
    date: datetime.date
    start_time: datetime.time
    end_time: datetime.time | None
    student_id: int | None
    student_name: str | None
    activity_name: str | None
    group_name: str | None
    title: str | None
    source_type: str
    source_id: int
    is_rescheduled: bool


_DUPLICATE_CHILD_MESSAGE: Final = "Ребёнок с таким ФИО и датой рождения уже добавлен."


def create_child(parent: Parent, data: dict[str, object]) -> Student:
    try:
        with transaction.atomic():
            return Student.objects.create(
                parent=parent,
                full_name=str(data["full_name"]),
                dob=cast("datetime.date", data["dob"]),
                school_grade=str(data.get("school_grade") or ""),
                health_issues=str(data.get("health_issues") or ""),
            )
    except IntegrityError as exc:
        # Сработал uq_student_active_per_parent_name_dob — дабл-сабмит формы
        raise _duplicate_child_error() from exc


def update_child(child: Student, data: dict[str, object]) -> Student:
    # ПОЧЕМУ свой перехват: DRF не строит проверку уникальности — поля parent
    # нет в сериализаторе. Без него переименование в «близнеца» давало 500
    for field, value in data.items():
        setattr(child, field, value)
    try:
        with transaction.atomic():
            child.save()
    except IntegrityError as exc:
        raise _duplicate_child_error() from exc
    return child


def _duplicate_child_error() -> ValidationError:
    return ValidationError(
        {"full_name": [_DUPLICATE_CHILD_MESSAGE]}, code="VALIDATION_ERROR"
    )


@dataclass(frozen=True, slots=True)
class ActiveEnrollmentView:
    id: int
    type: str
    status: str
    activity_name: str
    group_name: str
    subscription_id: int | None
    trial_date: datetime.date | None


class ChildHasActiveEnrollmentsError(Exception):
    def __init__(self, enrollments: list[ActiveEnrollmentView]) -> None:
        super().__init__(f"У ребёнка {len(enrollments)} живых записей.")
        self.enrollments = enrollments


def archive_child(parent: Parent, child_id: int) -> None:
    with transaction.atomic():
        # ПОЧЕМУ FOR NO KEY UPDATE: чекаут берёт тот же лок на ребёнка перед
        # созданием брони. Без него чекаут мог проверить «не архивный» и
        # записать ребёнка, которого мы в ту же секунду архивируем
        child = (
            Student.objects.active()
            .select_for_update(no_key=True)
            .filter(pk=child_id, parent=parent)
            .first()
        )
        if child is None:
            # Чужой, несуществующий и уже удалённый неотличимы
            raise NotFound(code="NOT_FOUND")
        blocking = _active_enrollments(child)
        if blocking:
            raise ChildHasActiveEnrollmentsError(blocking)
        # ПОЧЕМУ health_issues не стираем: решение бизнеса (2026-09-26) —
        # пока храним вместе с карточкой; стирание — отдельная задача по 152-ФЗ
        child.archived_at = timezone.now()
        child.save(update_fields=["archived_at", "updated_at"])


def _active_enrollments(child: Student) -> list[ActiveEnrollmentView]:
    # ПОЧЕМУ дата у пробного: оно остаётся ENROLLED и после визита
    # (uq_billing_active_regular_per_student_slot), прошедшее — уже история.
    # Действующий абонемент отдельно не проверяем: у живого абонемента всегда
    # есть HELD/ENROLLED-запись, истёкший их отменяет (sweep_expired_subscriptions)
    alive = (
        Q(status=EnrollmentStatus.HELD)
        | Q(status=EnrollmentStatus.ENROLLED, type=EnrollmentType.REGULAR)
        | Q(
            status=EnrollmentStatus.ENROLLED,
            type=EnrollmentType.TRIAL,
            trial_date__gte=timezone.localdate(),
        )
    )
    enrollments = (
        Enrollment.objects.filter(alive, student=child)
        .select_related("schedule__activity")
        .order_by("pk")
    )
    return [
        ActiveEnrollmentView(
            id=enrollment.pk,
            type=enrollment.type,
            status=enrollment.status,
            activity_name=enrollment.schedule.activity.name,
            group_name=enrollment.schedule.group_name,
            subscription_id=enrollment.subscription_id,
            trial_date=enrollment.trial_date,
        )
        for enrollment in enrollments
    ]


def parent_subscriptions_query(parent: Parent) -> QuerySet[Subscription]:
    # ПОЧЕМУ только ACTIVE/EXPIRED: в них абонемент попадает лишь после
    # оплаты (вебхук PENDING → ACTIVE, свипер ACTIVE → EXPIRED). PENDING и
    # CANCELED — незавершённые и брошенные оплаты, это не история покупок
    # ПОЧЕМУ записи всех статусов: при истечении свипер отменяет все записи
    # абонемента, а имя ребёнка и группы истории берутся именно из них
    enrollments_qs = Enrollment.objects.select_related(
        "student", "schedule__activity"
    ).order_by("pk")
    return (
        Subscription.objects.filter(parent=parent, status__in=_HISTORY_STATUSES)
        .prefetch_related("slots", Prefetch("enrollments", queryset=enrollments_qs))
        # Действующие сверху; -pk делает порядок однозначным — иначе при
        # равном created_at абонемент мог попасть на две страницы
        .order_by(
            Case(
                When(status=SubscriptionStatus.ACTIVE, then=0),
                default=1,
                output_field=IntegerField(),
            ),
            "-created_at",
            "-pk",
        )
    )


def build_subscription_views(
    subscriptions: Iterable[Subscription],
) -> list[SubscriptionView]:
    views: list[SubscriptionView] = []
    for subscription in subscriptions:
        slots = sorted(subscription.slots.all(), key=lambda slot: slot.pk)
        slots_by_schedule = {slot.slot_id: slot for slot in slots}
        enrollments = list(subscription.enrollments.all())
        # Абонемент всегда на одного ребёнка: чекаут принимает один student_id
        student_name = enrollments[0].student.full_name if enrollments else ""
        if subscription.status == SubscriptionStatus.ACTIVE:
            # ПОЧЕМУ: действующий — куда ребёнок ходит сейчас, только живые записи
            schedules = [
                enrollment.schedule
                for enrollment in enrollments
                if enrollment.status == EnrollmentStatus.ENROLLED
            ]
        else:
            # История — что было куплено: состав покупки лежит в слотах,
            # записи свипер уже отменил. Остаток фишек у истёкшего 0 — он
            # ушёл деньгами на депозит (_credit_unused_sessions)
            schedule_by_id = {
                enrollment.schedule_id: enrollment.schedule
                for enrollment in enrollments
            }
            schedules = [
                schedule_by_id[slot.slot_id]
                for slot in slots
                if slot.slot_id in schedule_by_id
            ]
        slot_views: list[SubscriptionSlotView] = []
        for schedule in schedules:
            slot = slots_by_schedule.get(schedule.pk)
            schedule_str = (
                f"{_WEEKDAY_ABBR_RU[schedule.day_of_week]} "
                f"{schedule.start_time:%H:%M}-{schedule.end_time:%H:%M}"
            )
            slot_views.append(
                SubscriptionSlotView(
                    schedule_id=schedule.pk,
                    activity_name=schedule.activity.name,
                    group_name=schedule.group_name,
                    schedule=schedule_str,
                    remaining_sessions=slot.remaining_tokens if slot is not None else 0,
                    total_sessions=slot.granted_tokens if slot is not None else 0,
                )
            )
        total_remaining = sum(sv.remaining_sessions for sv in slot_views)
        views.append(
            SubscriptionView(
                id=subscription.pk,
                display_id=f"#SUB-{subscription.pk}",
                status=subscription.status,
                student_name=student_name,
                purchase_price=subscription.purchase_price,
                created_at=subscription.created_at,
                start_date=subscription.start_date,
                expires_at=subscription.expires_at,
                total_remaining=total_remaining,
                slots=slot_views,
            )
        )
    return views


def get_parent_deposit_balance(parent: Parent) -> int:
    # Строки депозита нет до первого начисления — это баланс 0. При чтении её
    # не создаём: создание — дело начисления (_locked_parent_deposit)
    balance = (
        ParentDeposit.objects.filter(parent=parent)
        .values_list("balance", flat=True)
        .first()
    )
    return balance or 0


def parent_deposit_entries_query(
    parent: Parent,
) -> QuerySet[DepositEntry, Mapping[str, Any]]:
    # ПОЧЕМУ Coalesce: начисление при истечении ссылается на абонемент
    # напрямую, а списание и возврат — через транзакцию чекаута. Фронту
    # отдаём один subscription_id, склейка — одним LEFT JOIN в том же запросе
    return (
        DepositEntry.objects.filter(deposit__parent=parent)
        .annotate(
            source_subscription_id=Coalesce(
                "subscription_id", "transaction__subscription_id"
            )
        )
        .order_by("-created_at", "-pk")
        .values("pk", "amount", "reason", "created_at", "source_subscription_id")
    )


def build_deposit_entry_views(
    rows: Iterable[Mapping[str, Any]],
) -> list[DepositEntryView]:
    views: list[DepositEntryView] = []
    for row in rows:
        reason = row["reason"]
        subscription_id = cast("int | None", row["source_subscription_id"])
        views.append(
            DepositEntryView(
                id=row["pk"],
                amount=row["amount"],
                reason=reason,
                reason_display=_DEPOSIT_REASON_DISPLAY_OVERRIDES.get(
                    reason, DepositEntryReason(reason).label
                ),
                subscription_id=subscription_id,
                subscription_display_id=(
                    f"#SUB-{subscription_id}" if subscription_id is not None else None
                ),
                created_at=row["created_at"],
            )
        )
    return views


def build_upcoming_feed(
    parent: Parent, *, weeks: int, child_id: int | None = None
) -> list[UpcomingItem]:
    # ПОЧЕМУ: будущих занятий нет в БД — регулярные слоты проецируются на
    # даты горизонта и корректируются масками, как в публичной сетке
    start = timezone.localdate()
    end = start + datetime.timedelta(days=weeks * 7)

    enrollments = list(
        Enrollment.objects.filter(
            student__parent=parent,
            # ПОЧЕМУ: недельная проекция только для регулярных записей —
            # пробное живёт на конкретной дате и добавляется отдельно
            type=EnrollmentType.REGULAR,
            status=EnrollmentStatus.ENROLLED,
            schedule__is_active=True,
        )
        .select_related("student", "schedule__activity")
        .filter(**({"student_id": child_id} if child_id is not None else {}))
    )
    schedule_ids = {enrollment.schedule_id for enrollment in enrollments}
    masks = {
        (mask.schedule_id, mask.target_date): mask
        for mask in ScheduleMask.objects.filter(
            schedule_id__in=schedule_ids,
            target_date__gte=start,
            target_date__lt=end,
        )
    }

    items: list[UpcomingItem] = []
    for offset in range((end - start).days):
        day = start + datetime.timedelta(days=offset)
        for enrollment in enrollments:
            schedule = enrollment.schedule
            if schedule.day_of_week != day.weekday():
                continue
            mask = masks.get((schedule.pk, day))
            if mask is not None and mask.type == MaskType.CANCELLATION:
                continue
            is_rescheduled = mask is not None and mask.type == MaskType.RESCHEDULE
            start_time = schedule.start_time
            end_time: datetime.time | None = schedule.end_time
            if is_rescheduled and mask is not None and mask.new_start_time is not None:
                start_time = mask.new_start_time
                end_time = mask.new_end_time
            items.append(
                UpcomingItem(
                    kind="SUBSCRIPTION_SESSION",
                    date=day,
                    start_time=start_time,
                    end_time=end_time,
                    student_id=enrollment.student.pk,
                    student_name=enrollment.student.full_name,
                    activity_name=schedule.activity.name,
                    group_name=schedule.group_name,
                    title=None,
                    source_type="subscription",
                    # Сужение Optional: выборка ограничена REGULAR, у которых
                    # абонемент обязателен по ck_billing_enrollment_type_shape
                    source_id=cast(int, enrollment.subscription_id),
                    is_rescheduled=is_rescheduled,
                )
            )

    items.extend(_upcoming_trial_items(parent, start=start, end=end, child_id=child_id))
    if child_id is None:
        items.extend(_upcoming_event_items(parent, start=start, end=end))

    # ПОЧЕМУ хвост ключа: занятия двух детей в одно время иначе меняются
    # местами между запросами (выборка записей без order_by), и при
    # постраничной выдаче одно попадает на две страницы, а другое ни на одну
    items.sort(
        key=lambda item: (
            item.date,
            item.start_time,
            item.kind,
            item.student_id or 0,
            item.source_id,
        )
    )
    return items


def _upcoming_trial_items(
    parent: Parent,
    *,
    start: datetime.date,
    end: datetime.date,
    child_id: int | None,
) -> list[UpcomingItem]:
    # ПОЧЕМУ: только оплаченные (ENROLLED) — неоплаченная 15-минутная бронь
    # HELD может испариться и не должна попадать в календарь родителя
    trials = (
        Enrollment.objects.filter(
            student__parent=parent,
            type=EnrollmentType.TRIAL,
            status=EnrollmentStatus.ENROLLED,
            trial_date__gte=start,
            trial_date__lt=end,
        )
        .select_related("student", "schedule__activity")
        .filter(**({"student_id": child_id} if child_id is not None else {}))
    )
    return [
        UpcomingItem(
            kind="TRIAL",
            date=cast(datetime.date, trial.trial_date),
            start_time=trial.schedule.start_time,
            end_time=trial.schedule.end_time,
            student_id=trial.student.pk,
            student_name=trial.student.full_name,
            activity_name=trial.schedule.activity.name,
            group_name=trial.schedule.group_name,
            title=None,
            source_type="trial",
            source_id=trial.pk,
            is_rescheduled=False,
        )
        for trial in trials
    ]


def parent_trials_query(parent: Parent) -> QuerySet[Enrollment]:
    return (
        Enrollment.objects.filter(
            student__parent=parent,
            type=EnrollmentType.TRIAL,
        )
        .exclude(status=EnrollmentStatus.CANCELED)
        .select_related("student", "schedule__activity")
        .prefetch_related("transactions")
        .order_by("-trial_date", "-id")
    )


def build_trial_views(trials: Iterable[Enrollment]) -> list[TrialView]:
    # ПОЧЕМУ: cost — снапшот из транзакции покупки, а не текущая цена кружка;
    # прайс мог измениться после оформления
    views: list[TrialView] = []
    for trial in trials:
        cost: int | None = None
        for tx in trial.transactions.all():
            if tx.status in (TransactionStatus.PENDING, TransactionStatus.SUCCEEDED):
                cost = tx.amount
                break
        views.append(
            TrialView(
                id=trial.pk,
                student_id=trial.student.pk,
                student_name=trial.student.full_name,
                activity_name=trial.schedule.activity.name,
                group_name=trial.schedule.group_name,
                trial_date=cast(datetime.date, trial.trial_date),
                start_time=trial.schedule.start_time,
                end_time=trial.schedule.end_time,
                status=trial.status,
                cost=cost,
                created_at=trial.created_at,
            )
        )
    return views


def _upcoming_event_items(
    parent: Parent, *, start: datetime.date, end: datetime.date
) -> list[UpcomingItem]:
    # Гостевые регистрации дотягиваются в ЛК по совпадению номера телефона
    ownership = Q(parent=parent)
    if parent.phone:
        ownership |= Q(phone=parent.phone)
    registrations = (
        EventRegistration.objects.filter(
            ownership,
            status__in=SEAT_BLOCKING_STATUSES,
            event__start_datetime__date__gte=start,
            event__start_datetime__date__lt=end,
        )
        .select_related("event")
        .distinct()
    )
    items: list[UpcomingItem] = []
    for registration in registrations:
        event_start = timezone.localtime(registration.event.start_datetime)
        event_end = event_start + datetime.timedelta(
            minutes=registration.event.duration_minutes
        )
        items.append(
            UpcomingItem(
                kind="EVENT",
                date=event_start.date(),
                start_time=event_start.time(),
                end_time=event_end.time(),
                student_id=None,
                student_name=registration.child_name,
                activity_name=None,
                group_name=None,
                title=registration.event.title,
                source_type="event",
                source_id=registration.event_id,
                is_rescheduled=False,
            )
        )
    return items
