from __future__ import annotations

import logging
from typing import Literal

from apps.core.telegram import send_manager_message
from apps.public_forms.models import CallbackRequest, FeedbackRequest
from config.tkq import broker

logger = logging.getLogger(__name__)

FormType = Literal["callback", "feedback"]


async def _deliver(text: str, form_type: FormType, request_id: int) -> None:
    await send_manager_message(text, context=f"form={form_type} id={request_id}")


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
