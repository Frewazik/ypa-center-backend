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
from apps.schedule.models import Schedule
from apps.schedule.services import lesson_on_q, masks_without_lesson_on_target

logger = logging.getLogger(__name__)

_DEBIT_CHUNK_SIZE = 1_000
# ПОЧЕМУ неделя: у каждой группы в окне ровно одно занятие — сбой планировщика
# до недели добирается целиком. Дольше без журналов не заметить нельзя, а
# «пришёл всем» на давнем занятии — уже догадка, за которую списываются деньги
_CATCH_UP_DAYS = 6


def open_lesson(schedule_id: int, date: datetime.date) -> Lesson:
    # ПОЧЕМУ: занятие материализуется вместе с отметками «пришёл» на всех
    # записанных — посещаемость автоматическая, учитель только снимает
    # отсутствующих
    schedule = Schedule.objects.get(pk=schedule_id, is_active=True)
    if not Schedule.objects.filter(pk=schedule.pk).filter(lesson_on_q(date)).exists():
        raise ValidationError(
            {"date": ["На эту дату занятие группы не запланировано."]},
            code="VALIDATION_ERROR",
        )
    return _fill_lesson(schedule.pk, date)


def _fill_lesson(schedule_id: int, date: datetime.date) -> Lesson:
    day_start = timezone.make_aware(datetime.datetime.combine(date, datetime.time.min))
    with transaction.atomic():
        lesson, _ = Lesson.objects.get_or_create(schedule_id=schedule_id, date=date)
        # ПОЧЕМУ: пробный ученик попадает в журнал только на дату своего
        # визита — регулярная запись материализуется на каждое занятие в
        # сроке абонемента: добор прошлых дней не должен вписать абонемент,
        # начавшийся позже, или занятие после последнего дня
        enrolled = Enrollment.objects.filter(
            schedule_id=schedule_id, status=EnrollmentStatus.ENROLLED
        ).filter(
            Q(
                type=EnrollmentType.REGULAR,
                subscription__start_date__lte=date,
                subscription__expires_at__gte=day_start,
            )
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


def materialize_today_lessons(*, today: datetime.date | None = None) -> int:
    # ПОЧЕМУ добор прошлых дней: если утренний тик пропущен (воркер или
    # планировщик лежал), у занятия нет отметок — ночное списание ничего не
    # найдёт, и при истечении абонемента занятие уйдёт на депозит как
    # непосещённое
    day = today if today is not None else timezone.localdate()
    opened = 0
    for days_ago in range(_CATCH_UP_DAYS, -1, -1):
        opened += _open_lessons_on(
            day - datetime.timedelta(days=days_ago), catch_up=days_ago > 0
        )
    return opened


def _open_lessons_on(day: datetime.date, *, catch_up: bool) -> int:
    schedules = Schedule.objects.filter(is_active=True).filter(lesson_on_q(day))
    if catch_up:
        # ПОЧЕМУ: прошлое занятие — только если утро его не открыло. Повторное
        # открытие вписало бы купившего абонемент после начала занятия (его
        # первое занятие через неделю), и ночью за него сгорела бы фишка
        schedules = schedules.exclude(
            Exists(Lesson.objects.filter(schedule=OuterRef("pk"), date=day))
        )
    schedule_ids = list(schedules.order_by("pk").values_list("pk", flat=True))
    for schedule_id in schedule_ids:
        _fill_lesson(schedule_id, day)
    if schedule_ids and catch_up:
        logger.warning(
            "Утреннее открытие журнала за %s было пропущено, добрано занятий: %d",
            day,
            len(schedule_ids),
        )
    elif schedule_ids:
        logger.info("Открыто занятий на %s: %d", day, len(schedule_ids))
    return len(schedule_ids)


def debit_attended_lessons(*, today: datetime.date | None = None) -> int:
    # ПОЧЕМУ вечером, а не при открытии занятия: утром отметка «пришёл» —
    # только предположение, днём педагог снимает отсутствующих. Каждое
    # списание — своя транзакция: падение посередине оставляет уже списанное,
    # повторный запуск добирает остальное (token_debited + FOR UPDATE)
    day = today if today is not None else timezone.localdate()
    _release_missing_lessons(day)
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


def _release_missing_lessons(day: datetime.date) -> None:
    # ПОЧЕМУ: маску отмены или переноса на другой день могут поставить уже
    # после утреннего открытия занятия — отметки «пришёл» остались бы и сожгли
    # фишку за несостоявшееся занятие. Через сервис: если фишку успели
    # списать, она вернётся
    no_lesson = masks_without_lesson_on_target().filter(
        schedule=OuterRef("enrollment__schedule"), target_date=OuterRef("date")
    )
    attendance_ids = (
        Attendance.objects.filter(
            Exists(no_lesson), status=AttendanceStatus.ATTENDED, date__lte=day
        )
        # ПОЧЕМУ: списанную фишку истёкшего абонемента не вернуть
        # (TokenNotRefundableError) — без отсечки задача каждую ночь вечно
        # повторяла бы попытку и warning. Депозит тогда правит менеджер
        .filter(
            Q(token_debited=False)
            | Q(enrollment__subscription__status=SubscriptionStatus.ACTIVE)
        )
        .values_list("pk", flat=True)
    )
    for attendance_id in attendance_ids:
        try:
            set_attendance_status(
                attendance_id=attendance_id, status=AttendanceStatus.ABSENT_OK
            )
        except BillingError as exc:
            logger.warning(
                "Отметка #%s на несостоявшемся занятии не снята: %s",
                attendance_id,
                exc,
            )
