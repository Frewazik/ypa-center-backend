# TODO: backoff-middleware отложен, 5 линейных ретраев пока достаточно.
from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from taskiq import AsyncBroker, InMemoryBroker, SimpleRetryMiddleware, TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource


class TaskiqSettings(BaseSettings):
    # ПОЧЕМУ populate_by_name: validation_alias на redis_url запрещает
    # передачу поля по имени в конструктор — ломается явное создание в тестах
    model_config = SettingsConfigDict(env_prefix="TASKIQ_", populate_by_name=True)

    redis_url: str = Field(
        default="redis://localhost:6379/0",
        validation_alias=AliasChoices("TASKIQ_REDIS_URL", "REDIS_URL"),
    )
    use_inmemory_broker: bool = False
    default_retry_count: int = 5
    # ПОЧЕМУ: сообщение убитого воркера (OOM/SIGKILL) остаётся в PEL стрима
    # и через idle_timeout переезжает живому консьюмеру (XAUTOCLAIM).
    # Значение обязано превышать длительность самой долгой задачи, иначе
    # ещё живая задача уедет на повторное параллельное исполнение.
    reclaim_idle_timeout_ms: int = 600_000


def _build_broker(settings: TaskiqSettings) -> AsyncBroker:
    base: AsyncBroker
    if settings.use_inmemory_broker:
        base = InMemoryBroker()
    else:
        from taskiq_redis import RedisStreamBroker

        # ПОЧЕМУ: ListQueueBroker (BLPOP) отдаёт сообщение деструктивно —
        # SIGKILL воркера в середине обработки терял задачу безвозвратно.
        # Stream + consumer group подтверждают (XACK) только завершённые
        # задачи: семантика at-least-once, задачи обязаны быть идемпотентными.
        base = RedisStreamBroker(
            url=settings.redis_url,
            consumer_group_name="yra-workers",
            idle_timeout=settings.reclaim_idle_timeout_ms,
        )
    return base.with_middlewares(
        SimpleRetryMiddleware(default_retry_count=settings.default_retry_count)
    )


broker: AsyncBroker = _build_broker(TaskiqSettings())

scheduler = TaskiqScheduler(broker=broker, sources=[LabelScheduleSource(broker)])
