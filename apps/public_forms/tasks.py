from __future__ import annotations

import asyncio
import logging
from typing import Final, Literal

import httpx
from django.conf import settings
from django.urls import reverse

from apps.public_forms.models import CallbackRequest, FeedbackRequest
from config.tkq import broker

logger = logging.getLogger(__name__)

FormType = Literal["callback", "feedback"]

RETRY_AFTER_CAP_SECONDS = 30.0
RETRY_AFTER_DEFAULT_SECONDS = 1.0

_FORM_MODELS: Final[dict[FormType, type[CallbackRequest] | type[FeedbackRequest]]] = {
    "callback": CallbackRequest,
    "feedback": FeedbackRequest,
}
_FORM_TITLES: Final[dict[FormType, str]] = {
    "callback": "Новая заявка на обратный звонок",
    "feedback": "Новое обращение с сайта",
}
_ADMIN_CHANGE_VIEWS: Final[dict[FormType, str]] = {
    "callback": "admin:public_forms_callbackrequest_change",
    "feedback": "admin:public_forms_feedbackrequest_change",
}


class NotificationDeliveryError(Exception):
    pass


def build_notification_text(request_id: int, form_type: FormType) -> str:
    # ПОЧЕМУ: в Telegram только номер и ссылка, без имени, телефона и текста.
    # Серверы Telegram за рубежом — персональные данные клиента туда
    # не уходят (152-ФЗ, ст. 12), а заявку целиком менеджер открывает
    # в админке. Короткий текст заодно всегда влезает в лимит Telegram
    path = reverse(_ADMIN_CHANGE_VIEWS[form_type], args=[request_id])
    link = f"{settings.ADMIN_BASE_URL.rstrip('/')}{path}"
    return f"{_FORM_TITLES[form_type]} #{request_id}\n{link}"


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


async def _post_to_telegram(client: httpx.AsyncClient, text: str) -> httpx.Response:
    return await client.post(
        f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": settings.TELEGRAM_MANAGER_CHAT_ID, "text": text},
    )


async def _deliver(text: str, form_type: FormType, request_id: int) -> None:
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_MANAGER_CHAT_ID:
        # ПОЧЕМУ: канал не сконфигурирован, ретраить бессмысленно
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
                await asyncio.sleep(_retry_after_seconds(response))
                response = await _post_to_telegram(client, text)
    except httpx.HTTPError as exc:
        raise NotificationDeliveryError(
            f"Telegram недоступен (form={form_type} id={request_id})"
        ) from exc

    if response.status_code == 200:
        return
    if response.status_code == 429 or response.status_code >= 500:
        raise NotificationDeliveryError(
            f"Telegram ответил {response.status_code} (form={form_type} id={request_id})"
        )
    # ПОЧЕМУ: остальные ответы (неверный токен или чат, бота удалили
    # из чата) повтором не лечатся — пять ретраев только засорят очередь.
    # Нужна правка настроек, поэтому громко в лог и без исключения
    logger.error(
        "Уведомление не доставлено: Telegram отклонил запрос %s «%s» (form=%s id=%s)",
        response.status_code,
        _telegram_description(response),
        form_type,
        request_id,
    )


@broker.task(retry_on_error=True)
async def notify_managers_task(request_id: int, form_type: FormType) -> None:
    if not await _FORM_MODELS[form_type].objects.filter(pk=request_id).aexists():
        logger.error("Заявка не найдена (form=%s id=%s)", form_type, request_id)
        return
    await _deliver(
        build_notification_text(request_id, form_type), form_type, request_id
    )
