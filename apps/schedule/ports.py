# ПОЧЕМУ импорт из apps.billing.ports: в гексагональной схеме адаптер реализует
# порт, объявленный потребителем (billing). Обратной зависимости не возникает:
# billing.ports ссылается на этот модуль только строкой через import_string.
# Контракт порта: только чтение БД, никакого сетевого I/O — вызовы происходят
# внутри транзакции биллинга под advisory-локами.

from __future__ import annotations

import datetime
from typing import Final

from django.conf import settings
from django.utils import timezone

from apps.billing.ports import SlotTrialInfo, UnknownSlotError
from apps.schedule.models import Schedule, ScheduleMask
from apps.schedule.services import (
    is_lesson_bookable,
    lesson_on_q,
    lesson_session,
    normalize_week_start,
)

# ПОЧЕМУ: год сплошных отмен в реальности означает мёртвую группу;
# ограничение защищает от бесконечного сканирования при битых данных
_LOOKAHEAD_WEEKS: Final[int] = 53


class DjangoSchedulePort:
    def get_slot_capacity(self, slot_id: int) -> int:
        # ПОЧЕМУ: неактивная группа (или закрытый кружок) не продаётся —
        # для биллинга она неотличима от несуществующей
        capacity = (
            Schedule.objects.filter(
                pk=slot_id, is_active=True, activity__is_active=True
            )
            .values_list("max_capacity", flat=True)
            .first()
        )
        if capacity is None:
            raise UnknownSlotError(slot_id)
        return capacity

    def get_slot_trial_info(self, slot_id: int) -> SlotTrialInfo:
        row = (
            Schedule.objects.filter(
                pk=slot_id, is_active=True, activity__is_active=True
            )
            .values_list("activity_id", flat=True)
            .first()
        )
        if row is None:
            raise UnknownSlotError(slot_id)
        return SlotTrialInfo(activity_id=row, price_kopecks=settings.TRIAL_PRICE)

    def get_next_lesson_date(
        self, slot_id: int, after: datetime.datetime
    ) -> datetime.date:
        schedule = Schedule.objects.filter(
            pk=slot_id, is_active=True, activity__is_active=True
        ).first()
        if schedule is None:
            raise UnknownSlotError(slot_id)

        # ПОЧЕМУ localdate: `after` приходит в UTC, а неделя и время групп —
        # местные; .date() от UTC-момента до 07:00 дал бы вчерашний день
        first_week = normalize_week_start(timezone.localdate(after))
        horizon_end = first_week + datetime.timedelta(weeks=_LOOKAHEAD_WEEKS)
        # Все маски горизонта одним запросом; ключ target_date уникален
        # в пределах группы по констрейнту uniq_mask_per_schedule_per_date
        masks: dict[datetime.date, ScheduleMask] = {
            mask.target_date: mask
            for mask in ScheduleMask.objects.filter(
                schedule_id=slot_id,
                target_date__range=(first_week, horizon_end),
            )
        }

        for week_offset in range(_LOOKAHEAD_WEEKS):
            week_start = first_week + datetime.timedelta(weeks=week_offset)
            original = week_start + datetime.timedelta(days=schedule.day_of_week)
            # ПОЧЕМУ: перенос двигает и день, и время — граница записи
            # считается от фактического начала, как в сетке next-slots
            session = lesson_session(schedule, masks.get(original), original=original)
            if session is None:
                continue
            if is_lesson_bookable(session.date, session.start_time, now=after):
                return session.date

        raise UnknownSlotError(slot_id)

    def has_lesson_on(self, slot_id: int, on_date: datetime.date) -> bool:
        # ПОЧЕМУ без is_active: вопрос о прошедшем занятии — было ли оно,
        # а не продаётся ли группа сейчас
        return Schedule.objects.filter(pk=slot_id).filter(lesson_on_q(on_date)).exists()
