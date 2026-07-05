from __future__ import annotations

from taskiq import InMemoryBroker
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend

from config.settings import _env

_redis_url = str(_env.REDIS_URL)

broker: ListQueueBroker | InMemoryBroker

if _env.ENVIRONMENT == "local" and not _redis_url.startswith("redis"):
    # Fallback для юнит-тестов без Redis
    broker = InMemoryBroker()
else:
    broker = ListQueueBroker(url=_redis_url).with_result_backend(
        RedisAsyncResultBackend(redis_url=_redis_url)
    )
