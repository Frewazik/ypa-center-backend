from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.asyncio import ConnectionPool
from taskiq import AsyncTaskiqDecoratedTask, InMemoryBroker
from taskiq.exceptions import SendTaskError

from apps.core.queue import kiq_safely, kiq_sync

_received: list[str] = []
_broker = InMemoryBroker()


@_broker.task
def _record(value: str) -> None:
    _received.append(value)


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    # ПОЧЕМУ: у InMemoryBroker нет пула — подставляем пул как у Redis-брокера,
    # чтобы проверить сброс без живого Redis
    fake_pool = MagicMock(spec=ConnectionPool)
    monkeypatch.setattr(_broker, "connection_pool", fake_pool, raising=False)
    _received.clear()
    yield fake_pool
    _received.clear()


def _failing_task(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncTaskiqDecoratedTask[..., None]:
    monkeypatch.setattr(_record, "kiq", AsyncMock(side_effect=SendTaskError()))
    return _record


class TestKiqSync:
    def test_enqueues_task_and_resets_pool(self, pool: MagicMock) -> None:
        kiq_sync(_record, "a")

        assert _received == ["a"]
        pool.reset.assert_called_once_with()

    def test_broker_error_propagates_and_pool_is_reset(
        self, pool: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task = _failing_task(monkeypatch)

        with pytest.raises(SendTaskError):
            kiq_sync(task, "a")

        pool.reset.assert_called_once_with()


class TestKiqSafely:
    def test_broker_error_is_logged_not_raised_and_pool_is_reset(
        self,
        pool: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        task = _failing_task(monkeypatch)

        kiq_safely(task, "a")

        assert "Не удалось поставить задачу" in caplog.text
        pool.reset.assert_called_once_with()
