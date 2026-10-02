# ПОЧЕМУ: один канал уведомлений менеджерам для всех доменов (заявки с сайта,
# возвраты на ручной разбор) — лимиты Telegram и обработка 429 в одном месте

from __future__ import annotations

import asyncio
import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

RETRY_AFTER_CAP_SECONDS = 30.0


class TelegramDeliveryError(Exception):
    # ПОЧЕМУ: транзитный сбой — пробрасывается в брокер для ретрая задачи
    pass


def _http_client() -> httpx.AsyncClient:
    # ПОЧЕМУ: клиент живёт в рамках одного вызова — AsyncClient привязан
    # к event loop'у момента создания
    return httpx.AsyncClient(timeout=settings.EXTERNAL_HTTP_TIMEOUT_SECONDS)


async def _post(client: httpx.AsyncClient, text: str) -> httpx.Response:
    return await client.post(
        f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": settings.TELEGRAM_MANAGER_CHAT_ID, "text": text},
    )


async def send_manager_message(text: str, *, context: str) -> None:
    # context — метка для логов («form=callback id=7»), без ПД клиента
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_MANAGER_CHAT_ID:
        # ПОЧЕМУ: канал не сконфигурирован, ретраить бессмысленно,
        # а ПД клиента в лог не пишем
        logger.error("Уведомление не доставлено: Telegram не настроен (%s)", context)
        return

    try:
        async with _http_client() as client:
            response = await _post(client, text)
            if response.status_code == 429:
                # ПОЧЕМУ: sleep кооперативен — слот воркера ждёт, но event loop
                # свободен для остальных задач; ожидание жёстко ограничено капом,
                # а второй 429 уходит в ретрай брокера через исключение ниже
                retry_after = min(
                    float(response.headers.get("Retry-After", "1")),
                    RETRY_AFTER_CAP_SECONDS,
                )
                await asyncio.sleep(retry_after)
                response = await _post(client, text)
    except httpx.HTTPError as exc:
        raise TelegramDeliveryError(f"Telegram недоступен ({context})") from exc

    if response.status_code != 200:
        raise TelegramDeliveryError(
            f"Telegram ответил {response.status_code} ({context})"
        )
