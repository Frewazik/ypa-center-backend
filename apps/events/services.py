from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from apps.events.models import (
    SEAT_BLOCKING_STATUSES,
    Event,
    EventRegistration,
    RegistrationStatus,
)

if TYPE_CHECKING:
    from apps.users.models import Parent

logger = logging.getLogger(__name__)

HONEYPOT_FIELD: Final[str] = "website_url"
PENDING_PAYMENT_TTL: Final[datetime.timedelta] = datetime.timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class RegistrationSubmission:
    child_name: str
    parent_name: str
    phone: str
    email: str
    attendees_count: int
    source: str
    comment: str


def register_for_event(
    event_id: int, data: RegistrationSubmission, parent: Parent | None = None
) -> EventRegistration:
    # ПОЧЕМУ: остаток мест — денормализованный Event.seats_taken под
    # select_for_update. SUM по регистрациям в одном statement с FOR UPDATE
    # некорректен: READ COMMITTED + EvalPlanQual перечитывает только
    # залоченную строку, агрегат по старому снапшоту не видит
    # конкурентный коммит
    with transaction.atomic():
        try:
            event = Event.objects.select_for_update().get(
                pk=event_id, is_published=True
            )
        except Event.DoesNotExist as exc:
            raise NotFound("Событие не найдено или не опубликовано.") from exc

        if not event.is_upcoming:
            raise ValidationError(
                {"event": ["Регистрация на прошедшее событие закрыта."]},
                code="VALIDATION_ERROR",
            )

        if event.seats_free < data.attendees_count:
            raise ValidationError(
                {
                    "attendees_count": [
                        f"Недостаточно свободных мест: осталось {event.seats_free}."
                    ]
                },
                code="VALIDATION_ERROR",
            )

        registration = EventRegistration.objects.create(
            event=event,
            parent=parent,
            child_name=data.child_name,
            parent_name=data.parent_name,
            phone=data.phone,
            email=data.email,
            attendees_count=data.attendees_count,
            source=data.source,
            comment=data.comment,
            status=(
                RegistrationStatus.CONFIRMED
                if event.is_free
                else RegistrationStatus.PENDING_PAYMENT
            ),
        )
        event.seats_taken += data.attendees_count
        event.save(update_fields=["seats_taken"])
        return registration


def cancel_registration(registration_id: int) -> bool:
    with transaction.atomic():
        try:
            registration = EventRegistration.objects.get(pk=registration_id)
        except EventRegistration.DoesNotExist:
            return False

        # ПОЧЕМУ: единый порядок захвата — всегда Event первым,
        # иначе deadlock с параллельной регистрацией на это событие
        event = Event.objects.select_for_update().get(pk=registration.event_id)
        released = EventRegistration.objects.filter(
            pk=registration_id, status__in=SEAT_BLOCKING_STATUSES
        ).update(status=RegistrationStatus.CANCELED)
        if not released:
            return False

        event.seats_taken -= registration.attendees_count
        event.save(update_fields=["seats_taken"])
        return True


def release_expired_pending_registrations() -> int:
    deadline = timezone.now() - PENDING_PAYMENT_TTL
    expired_ids = list(
        EventRegistration.objects.filter(
            status=RegistrationStatus.PENDING_PAYMENT,
            created_at__lt=deadline,
        ).values_list("pk", flat=True)
    )
    # ПОЧЕМУ: транзакция на каждую бронь — короткие локи вместо одного
    # длинного на все события сразу
    released = sum(
        1 for registration_id in expired_ids if cancel_registration(registration_id)
    )
    if released:
        logger.info("Освобождено просроченных броней: %d", released)
    return released


def process_registration_submission(
    event_id: int, raw_data: dict[str, object], parent: Parent | None = None
) -> EventRegistration | None:
    if raw_data.get(HONEYPOT_FIELD):
        # ПОЧЕМУ: дропаем тихо — вьюха отдаст обычный успех,
        # и бот не узнает про ловушку
        logger.info("Honeypot сработал, регистрация на событие %s отброшена", event_id)
        return None
    payload = RegistrationSubmission(
        child_name=str(raw_data["child_name"]),
        parent_name=str(raw_data["parent_name"]),
        phone=str(raw_data["phone"]),
        email=str(raw_data.get("email") or ""),
        attendees_count=int(str(raw_data["attendees_count"])),
        source=str(raw_data.get("source") or ""),
        comment=str(raw_data.get("comment") or ""),
    )
    return register_for_event(event_id, payload, parent=parent)
