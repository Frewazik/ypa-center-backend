from __future__ import annotations

import logging

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from apps.core.telegram import send_manager_message
from apps.events.models import EventRegistration, RegistrationStatus
from apps.events.services import release_expired_pending_registrations
from config.tkq import broker

logger = logging.getLogger(__name__)


@broker.task(schedule=[{"cron": "*/10 * * * *"}])
async def release_expired_event_registrations_task() -> int:
    return await sync_to_async(release_expired_pending_registrations)()


@broker.task(retry_on_error=True, max_retries=5)
async def notify_paid_registration_task(registration_id: int) -> None:
    # ПОЧЕМУ: ставится строго из on_commit (services.confirm_paid_registration) —
    # после оплаты, а не при записи: неоплаченная бронь не требует действий
    # менеджера. Сбой Telegram (TelegramDeliveryError) уходит в ретрай брокера
    registration = (
        await EventRegistration.objects.select_related("event")
        .filter(pk=registration_id, status=RegistrationStatus.CONFIRMED)
        .afirst()
    )
    if registration is None:
        # ПОЧЕМУ: менеджер мог отменить бронь раньше, чем дошла очередь
        logger.info("Регистрация на событие %s: уже не подтверждена.", registration_id)
        return
    event = registration.event
    starts = timezone.localtime(event.start_datetime).strftime("%d.%m.%Y %H:%M")
    contacts = ", ".join(
        part for part in (str(registration.phone), registration.email) if part
    )
    # ПОЧЕМУ: имена и контакты в сообщении — решение владельца (менеджер
    # готовит места и при необходимости связывается с семьёй)
    text = (
        f"Оплачена онлайн запись на событие #{registration.pk}\n"
        f"«{event.title}», {starts}\n"
        f"Мест: {registration.attendees_count}\n"
        f"Ребёнок: {registration.child_name}\n"
        f"Родитель: {registration.parent_name}, {contacts}\n"
        "Админка → «Регистрации на события»"
    )
    await send_manager_message(text, context=f"event_registration id={registration.pk}")


@broker.task(retry_on_error=True, max_retries=3)
def send_registration_expired_email_task(registration_id: int) -> None:
    # ПОЧЕМУ: синхронная функция уводит SMTP I/O в тредпул.
    # Ставится строго из on_commit (services.expire_pending_registration)
    registration = (
        EventRegistration.objects.select_related("event")
        .filter(pk=registration_id, status=RegistrationStatus.CANCELED)
        .exclude(email="")
        .first()
    )
    if registration is None:
        logger.error(
            "Письмо о снятой брони: регистрация %s не найдена.", registration_id
        )
        return
    event = registration.event
    starts = timezone.localtime(event.start_datetime).strftime("%d.%m.%Y в %H:%M")
    send_mail(
        subject="Бронь на событие снята — «Улица Радости»",
        message=(
            f"Здравствуйте, {registration.parent_name}!\n\n"
            f"Ваша бронь на «{event.title}» {starts} не была подтверждена "
            f"вовремя, и места освободились.\n"
            f"Если вы всё ещё хотите прийти — запишитесь снова на сайте "
            f"или свяжитесь с нами."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[registration.email],
        fail_silently=False,
    )
