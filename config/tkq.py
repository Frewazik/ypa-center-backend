"""Брокер и планировщик Taskiq для всех доменов.

`TASKIQ_USE_INMEMORY_BROKER=true` — задачи in-process без Redis (тесты/CI).
Периодика: `taskiq scheduler config.tkq:scheduler`.

TODO: backoff-middleware отложен, 5 линейных ретраев пока достаточно.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from taskiq import AsyncBroker, InMemoryBroker, SimpleRetryMiddleware, TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource


class TaskiqSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TASKIQ_")

    redis_url: str = "redis://localhost:6379/0"
    use_inmemory_broker: bool = False
    default_retry_count: int = 5


def _build_broker(settings: TaskiqSettings) -> AsyncBroker:
    base: AsyncBroker
    if settings.use_inmemory_broker:
        base = InMemoryBroker()
    else:
        from taskiq_redis import ListQueueBroker

        base = ListQueueBroker(url=settings.redis_url)
    return base.with_middlewares(
        SimpleRetryMiddleware(default_retry_count=settings.default_retry_count)
    )


broker: AsyncBroker = _build_broker(TaskiqSettings())

scheduler = TaskiqScheduler(broker=broker, sources=[LabelScheduleSource(broker)])
