from __future__ import annotations

import datetime
import logging

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.billing.models import (
    Attendance,
    AttendanceStatus,
    Enrollment,
    EnrollmentStatus,
    EnrollmentType,
    SubscriptionStatus,
)
from apps.billing.selectors import attendances_awaiting_debit
from apps.billing.services import (
    BillingError,
    InsufficientTokensError,
    debit_token,
    set_attendance_status,
)
from apps.journal.models import Lesson
from apps.schedule.models import MaskType, Schedule, ScheduleMask

logger = logging.getLogger(__name__)

_DEBIT_CHUNK_SIZE = 1_000


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


def debit_attended_lessons(*, today: datetime.date | None = None) -> int:
    # ПОЧЕМУ вечером, а не при открытии занятия: утром отметка «пришёл» —
    # только предположение, днём педагог снимает отсутствующих. Каждое
    # списание — своя транзакция: падение посередине оставляет уже списанное,
    # повторный запуск добирает остальное (token_debited + FOR UPDATE)
    day = today if today is not None else timezone.localdate()
    _release_cancelled_lessons(day)
    candidate_ids = list(
        attendances_awaiting_debit()
        .filter(
            date__lte=day,
            enrollment__subscription__status=SubscriptionStatus.ACTIVE,
        )
        .order_by("date", "pk")
        .values_list("pk", flat=True)[:_DEBIT_CHUNK_SIZE]
    )
    debited = 0
    for attendance_id in candidate_ids:
        try:
            debit_token(attendance_id)
        except InsufficientTokensError:
            # ПОЧЕМУ: пятое занятие месяца при 4 фишках бесплатно (решение
            # бизнеса); после истечения абонемента строка выпадет из выборки
            logger.info("Отметка #%s: фишки слота кончились", attendance_id)
            continue
        except BillingError as exc:
            # ПОЧЕМУ: педагог снял «пришёл» или абонемент истёк между выборкой
            # и блокировкой — debit_token перепроверил под локом, пропускаем
            logger.warning("Отметка #%s не списана: %s", attendance_id, exc)
            continue
        debited += 1
    if debited:
        logger.info("Списано фишек за занятия по %s: %d", day, debited)
    return debited


def _release_cancelled_lessons(day: datetime.date) -> None:
    # ПОЧЕМУ: маску отмены могут поставить уже после утреннего открытия
    # занятия — отметки «пришёл» остались бы и сожгли фишку за несостоявшееся
    # занятие. Через сервис: если фишку успели списать, она вернётся
    cancelled = ScheduleMask.objects.filter(
        schedule=OuterRef("enrollment__schedule"),
        target_date=OuterRef("date"),
        type=MaskType.CANCELLATION,
    )
    attendance_ids = Attendance.objects.filter(
        Exists(cancelled), status=AttendanceStatus.ATTENDED, date__lte=day
    ).values_list("pk", flat=True)
    for attendance_id in attendance_ids:
        try:
            set_attendance_status(
                attendance_id=attendance_id, status=AttendanceStatus.ABSENT_OK
            )
        except BillingError as exc:
            logger.warning(
                "Отметка #%s на отменённом занятии не снята: %s", attendance_id, exc
            )


def _is_lesson_day(schedule: Schedule, date: datetime.date) -> bool:
    mask = ScheduleMask.objects.filter(schedule=schedule, target_date=date).first()
    if mask is not None:
        return mask.type != MaskType.CANCELLATION
    return schedule.day_of_week == date.weekday()
