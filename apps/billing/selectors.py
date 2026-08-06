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

from django.db.models import Q
from django.db.models.expressions import Combinable
from django.utils import timezone

from apps.billing.models import EnrollmentStatus, EnrollmentType

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
