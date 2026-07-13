from __future__ import annotations

import asyncio
import logging
import os
from typing import Literal

import httpx
from django.conf import settings
from taskiq import AsyncBroker, InMemoryBroker, SimpleRetryMiddleware
from taskiq_redis import ListQueueBroker

from apps.public_forms.models import CallbackRequest, FeedbackRequest

logger = logging.getLogger(__name__)

FormType = Literal["callback", "feedback"]

RETRY_AFTER_CAP_SECONDS = 30.0


class NotificationDeliveryError(Exception):
    pass


def _build_broker() -> AsyncBroker:
    # ПОЧЕМУ: воркер taskiq — отдельный процесс, его entrypoint обязан
    # вызвать django.setup() до импорта этого модуля
    base: AsyncBroker
    if os.getenv("ENVIRONMENT", "").lower() == "test":
        base = InMemoryBroker()
    else:
        base = ListQueueBroker(url=settings.TASKIQ_BROKER_URL)
    return base.with_middlewares(SimpleRetryMiddleware(default_retry_count=3))


broker = _build_broker()


async def _post_to_telegram(text: str) -> httpx.Response:
    # ПОЧЕМУ: ленивый импорт разрывает цикл tasks <-> services
    from apps.public_forms.services import get_http_client

    return await get_http_client().post(
        f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": settings.TELEGRAM_MANAGER_CHAT_ID, "text": text},
    )


async def _deliver(text: str, form_type: FormType, request_id: int) -> None:
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_MANAGER_CHAT_ID:
        # ПОЧЕМУ: канал не сконфигурирован, ретраить бессмысленно,
        # а ПД клиента в лог не пишем
        logger.error(
            "Уведомление не доставлено: Telegram не настроен (form=%s id=%s)",
            form_type,
            request_id,
        )
        return

    try:
        response = await _post_to_telegram(text)
        if response.status_code == 429:
            retry_after = min(
                float(response.headers.get("Retry-After", "1")), RETRY_AFTER_CAP_SECONDS
            )
            await asyncio.sleep(retry_after)
            response = await _post_to_telegram(text)
    except httpx.HTTPError as exc:
        raise NotificationDeliveryError(
            f"Telegram недоступен (form={form_type} id={request_id})"
        ) from exc

    if response.status_code != 200:
        raise NotificationDeliveryError(
            f"Telegram ответил {response.status_code} (form={form_type} id={request_id})"
        )


@broker.task(retry_on_error=True)
async def notify_managers_task(request_id: int, form_type: FormType) -> None:
    if form_type == "callback":
        try:
            callback = await CallbackRequest.objects.aget(pk=request_id)
        except CallbackRequest.DoesNotExist:
            logger.error("CallbackRequest id=%s не найдена", request_id)
            return
        text = (
            f"Заявка на обратный звонок #{callback.pk}\n"
            f"Имя: {callback.name}\n"
            f"Телефон: {callback.phone}\n"
            f"Удобное время: {callback.get_preferred_time_window_display()}"
        )
        await _deliver(text, "callback", callback.pk)
        return

    try:
        feedback = await FeedbackRequest.objects.aget(pk=request_id)
    except FeedbackRequest.DoesNotExist:
        logger.error("FeedbackRequest id=%s не найдено", request_id)
        return
    text = (
        f"Обращение с сайта #{feedback.pk}\n"
        f"Имя: {feedback.name or '—'}\n"
        f"Email: {feedback.email}\n"
        f"Сообщение: {feedback.message}"
    )
    await _deliver(text, "feedback", feedback.pk)