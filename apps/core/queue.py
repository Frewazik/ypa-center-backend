# ПОЧЕМУ: постановка задачи из синхронного кода (сервисы, on_commit-колбэки)
# повторялась в каждом приложении — сбой брокера и сброс пула в одном месте

from __future__ import annotations

import logging
from typing import Any

from asgiref.sync import async_to_sync
from taskiq import AsyncTaskiqDecoratedTask

logger = logging.getLogger(__name__)


def kiq_sync(task: AsyncTaskiqDecoratedTask[..., Any], *args: object) -> None:
    # Сбой брокера пробрасывается — для вебхука, которому нужен 500,
    # чтобы провайдер повторил уведомление
    try:
        async_to_sync(task.kiq)(*args)
    finally:
        # ПОЧЕМУ: async_to_sync закрывает созданный локальный event loop —
        # без сброса пул брокера переиспользует сокет мёртвого loop'а
        if hasattr(task.broker, "connection_pool"):
            task.broker.connection_pool.reset()


def kiq_safely(task: AsyncTaskiqDecoratedTask[..., Any], *args: object) -> None:
    # Вызывать из transaction.on_commit: без него воркер может прочитать
    # строку раньше коммита и не найти её
    try:
        kiq_sync(task, *args)
    except Exception:
        # ПОЧЕМУ: сбой брокера не должен откатывать уже сохранённые данные —
        # в on_commit-колбэке исключение ушло бы наружу из обработчика
        logger.exception("Не удалось поставить задачу %s", task.task_name)
