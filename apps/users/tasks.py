from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail
from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import ListQueueBroker

from apps.users.constants import OTP_TTL_MINUTES

logger = logging.getLogger(__name__)

# Брокер инициализируется здесь и импортируется в конфиг приложения.
#
# TASKIQ_BROKER_URL читается из настроек НАПРЯМУЮ, без getattr-дефолта:
# отсутствие переменной должно уронить приложение на старте (Fail Fast),
# а не молча подключить брокер к localhost и сливать задачи в пустоту.
# Валидация окружения — зона ответственности pydantic-settings; здесь мы
# лишь требуем, чтобы значение существовало.
broker: ListQueueBroker = ListQueueBroker(url=settings.TASKIQ_BROKER_URL)


@broker.task
def send_otp_email_task(email: str, code: str) -> None:
    """
    Фоновая задача: отправка письма с OTP-кодом.

    Объявлена как синхронная (def, не async def): send_mail выполняет
    блокирующий SMTP I/O. Вызов синхронного сетевого I/O внутри async def
    блокирует event loop воркера — он не сможет взять новые задачи до
    завершения SMTP-диалога. Taskiq запускает sync-задачи через
    ThreadPoolExecutor, изолируя блокировку от event loop.

    Запускается строго через transaction.on_commit() в services.py,
    чтобы гарантировать, что токен уже сохранён в БД перед отправкой.
    """
    subject = "Ваш код для входа в «Улицу Радости»"
    message = (
        f"Ваш одноразовый код для входа: {code}\n\n"
        f"Код действителен {OTP_TTL_MINUTES} минут.\n"
        f"Если вы не запрашивали код — просто проигнорируйте это письмо."
    )
    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=False,
    )


# Планировщик периодических задач. Источник расписаний — метки на самих
# задачах (schedule=[...] в декораторе). Запускается отдельным процессом:
#   taskiq scheduler apps.users.tasks:scheduler
scheduler: TaskiqScheduler = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker)],
)


@broker.task(schedule=[{"cron": "0 * * * *"}])
def purge_stale_otp_tokens_task() -> None:
    """
    Ежечасная очистка таблицы OTP-токенов (Thin Task):
    вся логика — в services.purge_stale_otp_tokens, задача лишь вызывает её.

    Импорт сервиса — локальный, внутри тела: services.py импортирует tasks.py
    (send_otp_email_task), top-level импорт в обратную сторону замкнул бы
    циклический импорт на старте приложения.

    Синхронная (def, не async def) по той же причине, что и send_otp_email_task:
    блокирующий БД I/O не должен висеть в event loop воркера.
    """
    from apps.users.services import purge_stale_otp_tokens

    result = purge_stale_otp_tokens()
    logger.info(
        "purge_stale_otp_tokens: expired_unused=%d retired_used=%d",
        result["expired_unused"],
        result["retired_used"],
    )
