from __future__ import annotations

from asgiref.sync import sync_to_async

from apps.events.services import release_expired_pending_registrations
from config.tkq import broker


@broker.task(schedule=[{"cron": "*/10 * * * *"}])
async def release_expired_event_registrations_task() -> int:
    return await sync_to_async(release_expired_pending_registrations)()
