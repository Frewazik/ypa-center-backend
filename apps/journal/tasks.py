from __future__ import annotations

from asgiref.sync import sync_to_async

from apps.journal.services import debit_attended_lessons, materialize_today_lessons
from config.tkq import broker


# ПОЧЕМУ: cron в UTC; 00:00 UTC = 07:00 Новосибирска — журнал дня готов
# до первого занятия
@broker.task(schedule=[{"cron": "0 0 * * *"}])
async def materialize_today_lessons_task() -> int:
    return await sync_to_async(materialize_today_lessons)()


# ПОЧЕМУ: 16:00 UTC = 23:00 Новосибирска — занятия дня закончились, а
# абонемент последнего дня ещё активен (истекает в 23:59:59 местного).
# Если тик пропущен, свипер истечения доберёт списание сам
@broker.task(schedule=[{"cron": "0 16 * * *"}])
async def debit_attended_lessons_task() -> int:
    return await sync_to_async(debit_attended_lessons)()
