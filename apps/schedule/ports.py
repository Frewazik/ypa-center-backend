# ПОЧЕМУ импорт из apps.billing.ports: в гексагональной схеме адаптер реализует
# порт, объявленный потребителем (billing). Обратной зависимости не возникает:
# billing.ports ссылается на этот модуль только строкой через import_string.
# Контракт порта: только чтение БД, никакого сетевого I/O — вызовы происходят
# внутри транзакции биллинга под advisory-локами.

from __future__ import annotations

import datetime
from typing import Final

from django.utils import timezone

from apps.billing.ports import SlotTrialInfo, UnknownSlotError
from apps.schedule.models import MaskType, Schedule, ScheduleMask
from apps.schedule.services import (
    _effective_session,
    is_lesson_bookable,
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
            .values_list("activity_id", "activity__price")
            .first()
        )
        if row is None:
            raise UnknownSlotError(slot_id)
        activity_id, price = row
        return SlotTrialInfo(activity_id=activity_id, price_kopecks=price)

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
            mask = masks.get(original)
            if mask is None:
                lesson_date, start_time = original, schedule.start_time
            elif mask.type == MaskType.CANCELLATION:
                continue
            else:
                # ПОЧЕМУ: перенос двигает и день, и время — граница записи
                # считается от фактического начала, как в сетке next-slots
                session = _effective_session(schedule, mask)
                lesson_date, start_time = session.date, session.start_time
            if is_lesson_bookable(lesson_date, start_time, now=after):
                return lesson_date

        raise UnknownSlotError(slot_id)
