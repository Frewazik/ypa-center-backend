from __future__ import annotations

import datetime
import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.billing.models import (
    Attendance,
    AttendanceStatus,
    Enrollment,
    EnrollmentStatus,
    EnrollmentType,
)
from apps.journal.models import Lesson
from apps.schedule.models import MaskType, Schedule, ScheduleMask

logger = logging.getLogger(__name__)


def open_lesson(schedule_id: int, date: datetime.date) -> Lesson:
    # ПОЧЕМУ: занятие материализуется вместе с отметками «пришёл» на всех
    # записанных — посещаемость автоматическая, учитель только снимает
    # отсутствующих
    schedule = Schedule.objects.get(pk=schedule_id, is_active=True)
    if not _is_lesson_day(schedule, date):
        raise ValidationError(
            {"date": ["На эту дату занятие группы не запланировано."]},
            code="VALIDATION_ERROR",
        )
    with transaction.atomic():
        lesson, _ = Lesson.objects.get_or_create(schedule=schedule, date=date)
        # ПОЧЕМУ: пробный ученик попадает в журнал только на дату своего
        # визита — регулярная запись материализуется на каждое занятие
        enrolled = Enrollment.objects.filter(
            schedule=schedule, status=EnrollmentStatus.ENROLLED
        ).filter(
            Q(type=EnrollmentType.REGULAR)
            | Q(type=EnrollmentType.TRIAL, trial_date=date)
        )
        # ПОЧЕМУ bulk_create: get_or_create в цикле давал 2 запроса на ученика
        # (SELECT + INSERT). ignore_conflicts опирается на
        # uq_billing_attendance_per_enrollment_date — повторный вызов на уже
        # открытом занятии не трогает выставленные учителем отметки
        Attendance.objects.bulk_create(
            [
                Attendance(
                    enrollment=enrollment,
                    date=date,
                    status=AttendanceStatus.ATTENDED,
                )
                for enrollment in enrolled
            ],
            ignore_conflicts=True,
        )
    return lesson


def materialize_today_lessons() -> int:
    today = timezone.localdate()
    schedules = Schedule.objects.filter(
        is_active=True, day_of_week=today.weekday()
    ).values_list("pk", flat=True)
    opened = 0
    for schedule_id in schedules:
        try:
            open_lesson(schedule_id, today)
        except ValidationError:
            continue
        opened += 1
    if opened:
        logger.info("Открыто занятий на %s: %d", today, opened)
    return opened


def _is_lesson_day(schedule: Schedule, date: datetime.date) -> bool:
    mask = ScheduleMask.objects.filter(schedule=schedule, target_date=date).first()
    if mask is not None:
        return mask.type != MaskType.CANCELLATION
    return schedule.day_of_week == date.weekday()
