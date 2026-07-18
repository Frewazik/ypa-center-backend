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
    Subscription,
    SubscriptionStatus,
)
from apps.events.models import SEAT_BLOCKING_STATUSES, EventRegistration
from apps.schedule.models import MaskType, ScheduleMask
from apps.users.models import Parent, Student

UPCOMING_DEFAULT_WEEKS: Final[int] = 4
UPCOMING_MAX_WEEKS: Final[int] = 8

UpcomingKind: TypeAlias = Literal["SUBSCRIPTION_SESSION", "EVENT"]


@dataclass(frozen=True, slots=True)
class SubscriptionSlotView:
    schedule_id: int
    activity_name: str
    group_name: str
    day_of_week: int
    start_time: datetime.time
    end_time: datetime.time
    student_id: int
    student_name: str
    remaining_sessions: int


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
    slots: list[SubscriptionSlotView]


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
        remaining_by_schedule = {
            slot.slot_id: slot.remaining_tokens for slot in subscription.slots.all()
        }
        slot_views: list[SubscriptionSlotView] = []
        student_name = ""
        for enrollment in subscription.enrollments.all():
            schedule = enrollment.schedule
            student_name = enrollment.student.full_name
            slot_views.append(
                SubscriptionSlotView(
                    schedule_id=schedule.pk,
                    activity_name=schedule.activity.name,
                    group_name=schedule.group_name,
                    day_of_week=schedule.day_of_week,
                    start_time=schedule.start_time,
                    end_time=schedule.end_time,
                    student_id=enrollment.student.pk,
                    student_name=enrollment.student.full_name,
                    remaining_sessions=remaining_by_schedule.get(schedule.pk, 0),
                )
            )
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
                    source_id=enrollment.subscription_id,
                    is_rescheduled=is_rescheduled,
                )
            )

    if child_id is None:
        items.extend(_upcoming_event_items(parent, start=start, end=end))

    items.sort(key=lambda item: (item.date, item.start_time))
    return items


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
