from __future__ import annotations

import pytest
from taskiq import InMemoryBroker
from taskiq_redis import RedisStreamBroker

from config.tkq import TaskiqSettings, _build_broker, broker


class TestBuildBroker:
    def test_stream_broker_by_default(self) -> None:
        # ПОЧЕМУ: контракт надежности — только stream-брокер с consumer
        # group даёт at-least-once; откат на BLPOP-список (at-most-once)
        # молча терял бы задачи убитых воркеров
        settings = TaskiqSettings(
            use_inmemory_broker=False, redis_url="redis://localhost:6379/0"
        )
        assert isinstance(_build_broker(settings), RedisStreamBroker)

    def test_inmemory_when_flag_set(self) -> None:
        settings = TaskiqSettings(use_inmemory_broker=True)
        assert isinstance(_build_broker(settings), InMemoryBroker)

    def test_redis_url_falls_back_to_shared_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ПОЧЕМУ: deployment задаёт только REDIS_URL; без фолбэка воркер
        # молча уходил бы на localhost и терял связь с брокером
        monkeypatch.delenv("TASKIQ_REDIS_URL", raising=False)
        monkeypatch.setenv("REDIS_URL", "redis://elsewhere:6380/2")
        assert TaskiqSettings().redis_url == "redis://elsewhere:6380/2"

    def test_explicit_taskiq_url_wins_over_shared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REDIS_URL", "redis://shared:6379/0")
        monkeypatch.setenv("TASKIQ_REDIS_URL", "redis://dedicated:6379/1")
        assert TaskiqSettings().redis_url == "redis://dedicated:6379/1"


class TestSingleBrokerRegistry:
    def test_all_domain_tasks_registered_on_shared_broker(self) -> None:
        # ПОЧЕМУ: воркер слушает один брокер; задача на собственном брокере
        # компилируется, но её сообщения никто не обработает
        from apps.billing.tasks import verify_and_process_payment
        from apps.events.tasks import release_expired_event_registrations_task
        from apps.journal.tasks import materialize_today_lessons_task
        from apps.public_forms.tasks import notify_managers_task
        from apps.users.tasks import purge_stale_otp_tokens_task, send_otp_email_task

        task_names = [
            verify_and_process_payment.task_name,
            release_expired_event_registrations_task.task_name,
            materialize_today_lessons_task.task_name,
            notify_managers_task.task_name,
            send_otp_email_task.task_name,
            purge_stale_otp_tokens_task.task_name,
        ]
        for task_name in task_names:
            assert broker.find_task(task_name) is not None
