from __future__ import annotations

import asyncio
import logging
from typing import Literal

import httpx
from django.conf import settings

from apps.public_forms.models import CallbackRequest, FeedbackRequest
from config.tkq import broker

logger = logging.getLogger(__name__)

FormType = Literal["callback", "feedback"]

RETRY_AFTER_CAP_SECONDS = 30.0


class NotificationDeliveryError(Exception):
    pass


async def _post_to_telegram(client: httpx.AsyncClient, text: str) -> httpx.Response:
    return await client.post(
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

    # ПОЧЕМУ: ленивый импорт разрывает цикл tasks <-> services
    from apps.public_forms.services import get_http_client

    try:
        async with get_http_client() as client:
            response = await _post_to_telegram(client, text)
            if response.status_code == 429:
                # ПОЧЕМУ: sleep кооперативен — слот воркера ждёт, но event loop
                # свободен для остальных задач; ожидание жёстко ограничено капом,
                # а второй 429 уходит в ретрай брокера через исключение ниже
                retry_after = min(
                    float(response.headers.get("Retry-After", "1")),
                    RETRY_AFTER_CAP_SECONDS,
                )
                await asyncio.sleep(retry_after)
                response = await _post_to_telegram(client, text)
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
