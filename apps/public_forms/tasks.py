from __future__ import annotations

import logging
from typing import Final, Literal

from django.conf import settings
from django.urls import reverse

from apps.core.telegram import send_manager_message
from apps.public_forms.models import CallbackRequest, FeedbackRequest
from config.tkq import broker

logger = logging.getLogger(__name__)

FormType = Literal["callback", "feedback"]

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


def build_notification_text(request_id: int, form_type: FormType) -> str:
    # ПОЧЕМУ: в Telegram только номер и ссылка, без имени, телефона и текста.
    # Серверы Telegram за рубежом — персональные данные клиента туда
    # не уходят (152-ФЗ, ст. 12), а заявку целиком менеджер открывает
    # в админке. Короткий текст заодно всегда влезает в лимит Telegram
    path = reverse(_ADMIN_CHANGE_VIEWS[form_type], args=[request_id])
    link = f"{settings.ADMIN_BASE_URL.rstrip('/')}{path}"
    return f"{_FORM_TITLES[form_type]} #{request_id}\n{link}"


@broker.task(retry_on_error=True)
async def notify_managers_task(request_id: int, form_type: FormType) -> None:
    if not await _FORM_MODELS[form_type].objects.filter(pk=request_id).aexists():
        logger.error("Заявка не найдена (form=%s id=%s)", form_type, request_id)
        return
    await send_manager_message(
        build_notification_text(request_id, form_type),
        context=f"form={form_type} id={request_id}",
    )
