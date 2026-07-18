from __future__ import annotations

from asgiref.sync import sync_to_async

from apps.journal.services import materialize_today_lessons
from config.tkq import broker


# ПОЧЕМУ: cron в UTC; 00:00 UTC = 07:00 Новосибирска — журнал дня готов
# до первого занятия
@broker.task(schedule=[{"cron": "0 0 * * *"}])
async def materialize_today_lessons_task() -> int:
    return await sync_to_async(materialize_today_lessons)()
