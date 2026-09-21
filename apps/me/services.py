from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Final, Literal, TypeAlias, cast

from django.db import IntegrityError, transaction
from django.db.models import Prefetch, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.billing.models import (
    Enrollment,
    EnrollmentStatus,
    EnrollmentType,
    Subscription,
    SubscriptionStatus,
    TransactionStatus,
)
from apps.events.models import SEAT_BLOCKING_STATUSES, EventRegistration
from apps.schedule.models import MaskType, ScheduleMask
from apps.users.models import Parent, Student

UPCOMING_DEFAULT_WEEKS: Final[int] = 4
UPCOMING_MAX_WEEKS: Final[int] = 8

UpcomingKind: TypeAlias = Literal["SUBSCRIPTION_SESSION", "TRIAL", "EVENT"]


_WEEKDAY_ABBR_RU: Final = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")


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
        # Сработал uq_student_per_parent_name_dob — дабл-сабмит формы
        raise ValidationError(
            {"full_name": ["Ребёнок с таким ФИО и датой рождения уже добавлен."]},
            code="VALIDATION_ERROR",
        ) from exc


def list_parent_subscriptions(parent: Parent) -> list[SubscriptionView]:
    enrollments_qs = Enrollment.objects.filter(
        status=EnrollmentStatus.ENROLLED
    ).select_related("student", "schedule__activity")
    subscriptions = (
        Subscription.objects.filter(parent=parent)
        .exclude(status=SubscriptionStatus.DRAFT)
        .prefetch_related("slots", Prefetch("enrollments", queryset=enrollments_qs))
        .order_by("-created_at")
    )

    views: list[SubscriptionView] = []
    for subscription in subscriptions:
        slots_by_schedule = {slot.slot_id: slot for slot in subscription.slots.all()}
        slot_views: list[SubscriptionSlotView] = []
        student_name = ""
        for enrollment in subscription.enrollments.all():
            schedule = enrollment.schedule
            student_name = enrollment.student.full_name
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

    items.sort(key=lambda item: (item.date, item.start_time))
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


def list_parent_trials(parent: Parent) -> list[TrialView]:
    # ПОЧЕМУ: cost — снапшот из транзакции покупки, а не текущая цена кружка;
    # прайс мог измениться после оформления
    trials = (
        Enrollment.objects.filter(
            student__parent=parent,
            type=EnrollmentType.TRIAL,
        )
        .exclude(status=EnrollmentStatus.CANCELED)
        .select_related("student", "schedule__activity")
        .prefetch_related("transactions")
        .order_by("-trial_date", "-id")
    )
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
