# ПОЧЕМУ: один канал уведомлений менеджерам для всех доменов (заявки с сайта,
# возвраты на ручной разбор) — лимиты Telegram и обработка 429 в одном месте

from __future__ import annotations

import asyncio
import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

RETRY_AFTER_CAP_SECONDS = 30.0
RETRY_AFTER_DEFAULT_SECONDS = 1.0


class TelegramDeliveryError(Exception):
    # ПОЧЕМУ: транзитный сбой — пробрасывается в брокер для ретрая задачи
    pass


def _http_client() -> httpx.AsyncClient:
    # ПОЧЕМУ: клиент живёт в рамках одного вызова — AsyncClient привязан
    # к event loop'у момента создания
    return httpx.AsyncClient(timeout=settings.EXTERNAL_HTTP_TIMEOUT_SECONDS)


def _retry_after_seconds(response: httpx.Response) -> float:
    # ПОЧЕМУ: Telegram сообщает паузу в теле ответа (parameters.retry_after);
    # заголовок Retry-After — запасной вариант. Мусор в ответе не должен
    # ронять задачу ValueError'ом
    raw: str | float | None = response.headers.get("Retry-After")
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        parameters = payload.get("parameters")
        if isinstance(parameters, dict) and "retry_after" in parameters:
            raw = parameters["retry_after"]
    if raw is None:
        return RETRY_AFTER_DEFAULT_SECONDS
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return RETRY_AFTER_DEFAULT_SECONDS
    return min(max(seconds, 0.0), RETRY_AFTER_CAP_SECONDS)


def _telegram_description(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    if isinstance(payload, dict):
        return str(payload.get("description", ""))
    return ""


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
                await asyncio.sleep(_retry_after_seconds(response))
                response = await _post(client, text)
    except httpx.HTTPError as exc:
        raise TelegramDeliveryError(f"Telegram недоступен ({context})") from exc

    if response.status_code == 200:
        return
    if response.status_code == 429 or response.status_code >= 500:
        raise TelegramDeliveryError(
            f"Telegram ответил {response.status_code} ({context})"
        )
    # ПОЧЕМУ: остальные ответы (неверный токен или чат, бота удалили
    # из чата) повтором не лечатся — пять ретраев только засорят очередь.
    # Нужна правка настроек, поэтому громко в лог и без исключения
    logger.error(
        "Уведомление не доставлено: Telegram отклонил запрос %s «%s» (%s)",
        response.status_code,
        _telegram_description(response),
        context,
    )
