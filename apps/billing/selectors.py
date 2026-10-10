# ПОЧЕМУ отдельный модуль: правило «кто занимает место в группе» — знание
# биллинга, но нужно оно и расписанию, и витрине. Раньше каждый домен
# конструировал свой Q с хардкодом статусов и типов записи: три копии одного
# инварианта, которые разъезжались при любой правке схемы.
#
# ПОЧЕМУ селекторы разделены на regular/trial: у потребителей семантически
# разные вопросы. Каталог спрашивает «насколько укомплектована группа» —
# это постоянный состав, разовый визитёр карточку не гасит. Недельная сетка
# спрашивает «сколько мест на этом занятии» — там дата известна и пробные
# считаются строго на неё. Единый счётчик на оба вопроса давал фантомные
# sold-out на витрине.
#
# Потребители подставляют свой префикс JOIN-пути (schedule → "enrollment__").

from __future__ import annotations

import datetime
from collections.abc import Iterable

from django.db.models import Q, QuerySet
from django.db.models.expressions import Combinable
from django.utils import timezone

from apps.billing.models import (
    Attendance,
    AttendanceStatus,
    Enrollment,
    EnrollmentStatus,
    EnrollmentType,
)

# ПОЧЕМУ: неоплаченная бронь держит место ровно столько, сколько живёт
# транзакция; значение обязано совпадать с _PENDING_TRANSACTION_TTL
HOLD_TTL = datetime.timedelta(minutes=15)


def active_seat_q(prefix: str = "") -> Q:
    """Записи, которые вообще претендуют на место: оплаченные и живые брони."""
    field = f"{prefix}%s"
    hold_alive_after = timezone.now() - HOLD_TTL
    return Q(**{field % "status": EnrollmentStatus.ENROLLED}) | Q(
        **{
            field % "status": EnrollmentStatus.HELD,
            field % "created_at__gte": hold_alive_after,
        }
    )


def regular_seat_q(prefix: str = "") -> Q:
    """Постоянные записи — держат место на каждом занятии слота."""
    return active_seat_q(prefix) & Q(**{f"{prefix}type": EnrollmentType.REGULAR})


def trial_seat_q(
    prefix: str = "",
    *,
    on_date: datetime.date | Combinable | None = None,
) -> Q:
    """Пробные — держат место ровно один календарный день.

    on_date задана — пробные строго этого дня. Принимается и выражение
    (WeekSessionDate), чтобы сравнивать с датой строки прямо в SQL; тогда
    отсечка «не раньше сегодня» не нужна и сетка за прошлую неделю остаётся
    корректной. Не задана — все ещё не состоявшиеся; вызывающий сам решает,
    как их агрегировать (пик по дням для абонемента).
    """
    field = f"{prefix}%s"
    conditions: dict[str, object] = {field % "type": EnrollmentType.TRIAL}
    if on_date is not None:
        conditions[field % "trial_date"] = on_date
    else:
        conditions[field % "trial_date__gte"] = timezone.localdate()
    return active_seat_q(prefix) & Q(**conditions)


def attendances_awaiting_debit() -> QuerySet[Attendance]:
    """Отметки «пришёл» по абонементу, за которые фишка ещё не списана.

    Один запрос на ночную задачу журнала и на добор в свипере истечения:
    правило «что списывать» не должно разъехаться между ними. Списывает
    только «пришёл» — пропуск с любой причиной фишку не сжигает (решение
    бизнеса, project-context §5). Пробные отсечены: фишек у них нет.
    """
    return Attendance.objects.filter(
        status=AttendanceStatus.ATTENDED,
        token_debited=False,
        enrollment__status=EnrollmentStatus.ENROLLED,
        enrollment__subscription__isnull=False,
    )


def lesson_seat_holders(
    schedule_ids: Iterable[int], *, on_date: datetime.date | None
) -> QuerySet[Enrollment]:
    """Кого касается правка занятия: постоянный состав групп и пробные.

    on_date задана — пробные строго на эту дату (разовая отмена или перенос).
    Не задана — все ещё не состоявшиеся пробные (группа переезжает насовсем).
    """
    return (
        Enrollment.objects.filter(schedule_id__in=list(schedule_ids))
        .filter(regular_seat_q() | trial_seat_q(on_date=on_date))
        .select_related("student__parent", "schedule__activity")
        .order_by("schedule_id", "student__full_name", "pk")
    )
