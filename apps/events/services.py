from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from apps.core.queue import kiq_safely
from apps.events.models import (
    SEAT_BLOCKING_STATUSES,
    Event,
    EventRegistration,
    RegistrationStatus,
)
from apps.users.consent import ConsentSource, record_consent
from apps.users.models import ConsentPurpose

if TYPE_CHECKING:
    from apps.users.models import Parent

logger = logging.getLogger(__name__)

HONEYPOT_FIELD: Final[str] = "website_url"
# ПОЧЕМУ: потолок мест на одну заявку — без него один запрос забирал
# полсобытия (выкуп бесплатных мест скриптом)
MAX_ATTENDEES_PER_REGISTRATION: Final[int] = 5


def pending_payment_ttl() -> datetime.timedelta:
    return datetime.timedelta(minutes=settings.EVENT_PENDING_PAYMENT_TTL_MINUTES)


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
    event_id: int,
    data: RegistrationSubmission,
    parent: Parent | None = None,
    consent: ConsentSource | None = None,
) -> EventRegistration:
    # consent=None — регистрацию заводит сотрудник (админка, звонок), согласие
    # на сайте не давалось; с сайта вьюха всегда передаёт источник согласия
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

        # ПОЧЕМУ: проверка под локом события — параллельная заявка с тем же
        # номером ждёт его и увидит эту; индекс uq_event_registration_active_phone
        # — страховка на случай записи в обход сервиса
        if EventRegistration.objects.filter(
            event=event, phone=data.phone, status__in=SEAT_BLOCKING_STATUSES
        ).exists():
            raise ValidationError(
                {"phone": ["На это событие с этим номером уже есть запись."]},
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
        if consent is not None:
            record_consent(
                ConsentPurpose.EVENT_REGISTRATION,
                consent,
                parent=parent,
                email=data.email,
                phone=data.phone,
                source_id=registration.pk,
            )
        if not event.is_free:
            # ПОЧЕМУ: оплата на месте — пока менеджер не нажал «Подтвердить
            # оплату», бронь снимется по TTL; без сообщения о ней никто не знал
            _schedule_task("notify_new_registration_task", registration.pk)
        return registration


def _release_seats(
    registration_id: int, from_statuses: tuple[str, ...]
) -> EventRegistration | None:
    # Отменяет регистрацию, только если она ещё в одном из from_statuses,
    # и возвращает места событию. None — отменять было нечего
    with transaction.atomic():
        try:
            registration = EventRegistration.objects.get(pk=registration_id)
        except EventRegistration.DoesNotExist:
            return None

        # ПОЧЕМУ: единый порядок захвата — всегда Event первым,
        # иначе deadlock с параллельной регистрацией на это событие
        event = Event.objects.select_for_update().get(pk=registration.event_id)
        # ПОЧЕМУ: статус проверяется в самом UPDATE, а не по прочитанному выше —
        # строку мог поменять параллельный экшен админки; Postgres дождётся
        # его коммита и перепроверит условие
        released = EventRegistration.objects.filter(
            pk=registration_id, status__in=from_statuses
        ).update(status=RegistrationStatus.CANCELED)
        if not released:
            return None

        event.seats_taken -= registration.attendees_count
        event.save(update_fields=["seats_taken"])
        return registration


def cancel_registration(registration_id: int) -> bool:
    return _release_seats(registration_id, SEAT_BLOCKING_STATUSES) is not None


def expire_pending_registration(registration_id: int) -> bool:
    # ПОЧЕМУ: только PENDING_PAYMENT — общая cancel_registration отменила бы и
    # бронь, которую менеджер подтвердил, пока свипер шёл по списку
    with transaction.atomic():
        registration = _release_seats(
            registration_id, (RegistrationStatus.PENDING_PAYMENT,)
        )
        if registration is not None and registration.email:
            _schedule_task("send_registration_expired_email_task", registration.pk)
    return registration is not None


def confirm_registration(registration_id: int) -> bool:
    with transaction.atomic():
        try:
            registration = EventRegistration.objects.get(pk=registration_id)
        except EventRegistration.DoesNotExist:
            return False

        # ПОЧЕМУ: тот же порядок, что у отмены, — событие, потом регистрация.
        # Места не меняются (PENDING_PAYMENT и CONFIRMED оба их занимают),
        # но отмена и подтверждение одной брони выстраиваются в очередь
        Event.objects.select_for_update().get(pk=registration.event_id)
        return bool(
            EventRegistration.objects.filter(
                pk=registration_id,
                status__in=(
                    RegistrationStatus.NEW,
                    RegistrationStatus.PENDING_PAYMENT,
                ),
            ).update(status=RegistrationStatus.CONFIRMED)
        )


def release_expired_pending_registrations() -> int:
    deadline = timezone.now() - pending_payment_ttl()
    expired_ids = list(
        EventRegistration.objects.filter(
            status=RegistrationStatus.PENDING_PAYMENT,
            created_at__lt=deadline,
        ).values_list("pk", flat=True)
    )
    # ПОЧЕМУ: транзакция на каждую бронь — короткие локи вместо одного
    # длинного на все события сразу
    released = sum(
        1
        for registration_id in expired_ids
        if expire_pending_registration(registration_id)
    )
    if released:
        logger.info("Освобождено просроченных броней: %d", released)
    return released


def process_registration_submission(
    event_id: int,
    raw_data: dict[str, object],
    consent: ConsentSource,
    parent: Parent | None = None,
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
    return register_for_event(event_id, payload, parent=parent, consent=consent)


def _schedule_task(task_name: str, registration_id: int) -> None:
    transaction.on_commit(lambda: _enqueue_task(task_name, registration_id))


def _enqueue_task(task_name: str, registration_id: int) -> None:
    # ПОЧЕМУ: локальный импорт — tasks импортирует services на уровне модуля
    from apps.events import tasks

    kiq_safely(getattr(tasks, task_name), registration_id)
