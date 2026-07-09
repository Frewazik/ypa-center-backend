from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail
from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import ListQueueBroker

from apps.users.constants import OTP_TTL_MINUTES

logger = logging.getLogger(__name__)

# ПОЧЕМУ: читаем напрямую без getattr; отсутствие урла должно жестко уронить приложение на старте
broker: ListQueueBroker = ListQueueBroker(url=settings.TASKIQ_BROKER_URL)


@broker.task
def send_otp_email_task(email: str, code: str) -> None:
    # ПОЧЕМУ: синхронная функция уводит SMTP I/O в тредпул.
    # Вызывать строго через transaction.on_commit()
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


scheduler: TaskiqScheduler = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker)],
)


@broker.task(schedule=[{"cron": "0 * * * *"}])
def purge_stale_otp_tokens_task() -> None:
    # ПОЧЕМУ: локальный импорт предотвращает циклическую зависимость (services импортирует tasks)
    from apps.users.services import purge_stale_otp_tokens

    result = purge_stale_otp_tokens()
    logger.info(
        "purge_stale_otp_tokens: expired_unused=%d retired_used=%d",
        result["expired_unused"],
        result["retired_used"],
    )
