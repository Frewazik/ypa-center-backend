from __future__ import annotations

import logging
from typing import Any, Final, TypedDict

import httpx
from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings
from django.db import transaction
from rest_framework.serializers import ValidationError

from apps.public_forms.models import CallbackRequest, FeedbackRequest
from apps.public_forms.tasks import FormType, notify_managers_task

logger = logging.getLogger(__name__)

HONEYPOT_FIELD: Final[str] = "website_url"

_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    # ПОЧЕМУ: общий клиент переиспользует пул соединений вместо
    # TCP+TLS handshake на каждый запрос
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=settings.EXTERNAL_HTTP_TIMEOUT_SECONDS)
    return _http_client


class CallbackSubmission(TypedDict):
    name: str
    phone: str
    preferred_time_window: str


class FeedbackSubmission(TypedDict):
    name: str
    email: str
    message: str


async def verify_captcha_token(token: str, remote_ip: str | None) -> bool:
    if not token:
        return False
    try:
        response = await get_http_client().post(
            settings.CAPTCHA_VERIFY_URL,
            data={
                "secret": settings.CAPTCHA_SECRET_KEY,
                "response": token,
                "remoteip": remote_ip or "",
            },
        )
    except httpx.HTTPError:
        logger.warning("Провайдер капчи недоступен, токен отклонён")
        return False

    if response.status_code != 200:
        return False
    try:
        payload = response.json()
    except ValueError:
        # ПОЧЕМУ: WAF провайдера может отдать 200 с HTML-заглушкой
        # вместо JSON, и json() уронит запрос в 500
        return False
    return isinstance(payload, dict) and payload.get("success") is True


def _enqueue_notification(request_id: int, form_type: FormType) -> None:
    async_to_sync(notify_managers_task.kiq)(request_id, form_type)


def _schedule_manager_notification(request_id: int, form_type: FormType) -> None:
    # ПОЧЕМУ: без on_commit воркер может прочитать заявку раньше
    # коммита транзакции и не найти её в БД
    transaction.on_commit(lambda: _enqueue_notification(request_id, form_type))


def _create_callback(data: CallbackSubmission) -> CallbackRequest:
    # ПОЧЕМУ: без явного atomic на автокоммите on_commit сработал бы
    # мгновенно, до завершения функции
    with transaction.atomic():
        instance = CallbackRequest.objects.create(
            name=data["name"],
            phone=data["phone"],
            preferred_time_window=data["preferred_time_window"],
        )
        _schedule_manager_notification(instance.pk, "callback")
    return instance


def _create_feedback(data: FeedbackSubmission) -> FeedbackRequest:
    with transaction.atomic():
        instance = FeedbackRequest.objects.create(
            name=data["name"],
            email=data["email"],
            message=data["message"],
        )
        _schedule_manager_notification(instance.pk, "feedback")
    return instance


async def _passes_spam_gate(raw_data: dict[str, Any], remote_ip: str | None) -> bool:
    if raw_data.get(HONEYPOT_FIELD):
        # ПОЧЕМУ: дропаем тихо — вьюха отдаст обычный успех,
        # и бот не узнает про ловушку
        logger.info("Honeypot сработал, заявка отброшена")
        return False

    token = str(raw_data.get("captcha_token") or "")
    if not await verify_captcha_token(token=token, remote_ip=remote_ip):
        raise ValidationError({"captcha_token": ["Проверка капчи не пройдена."]})
    return True


async def process_callback_submission(
    raw_data: dict[str, Any], remote_ip: str | None
) -> CallbackRequest | None:
    if not await _passes_spam_gate(raw_data, remote_ip):
        return None
    payload: CallbackSubmission = {
        "name": raw_data["name"],
        "phone": str(raw_data["phone"]),
        "preferred_time_window": raw_data["preferred_time_window"],
    }
    return await sync_to_async(_create_callback)(payload)


async def process_feedback_submission(
    raw_data: dict[str, Any], remote_ip: str | None
) -> FeedbackRequest | None:
    if not await _passes_spam_gate(raw_data, remote_ip):
        return None
    payload: FeedbackSubmission = {
        "name": raw_data.get("name", ""),
        "email": raw_data["email"],
        "message": raw_data["message"],
    }
    return await sync_to_async(_create_feedback)(payload)