# ПОЧЕМУ: жесткая изоляция доменов. Billing ничего не знает про ORM apps.schedule,
# связывание инжектится через BILLING_SCHEDULE_PORT_CLASS

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from django.utils.module_loading import import_string
from pydantic_settings import BaseSettings, SettingsConfigDict


class UnknownSlotError(Exception):
    def __init__(self, slot_id: int) -> None:
        super().__init__(f"Слот {slot_id} не найден в расписании.")
        self.slot_id = slot_id


@dataclass(frozen=True, slots=True)
class SlotTrialInfo:
    # ПОЧЕМУ: пробное тарифицируется ценой кружка, а лимит «1 пробное на
    # ребёнка» действует на уровне кружка — обе величины принадлежат чужому
    # домену и добываются только через порт
    activity_id: int
    price_kopecks: int


@runtime_checkable
class SchedulePort(Protocol):
    # !!!: Вызывается внутри транзакции под advisory-локами.
    # Реализация ОБЯЗАНА работать без сетевого I/O,
    # иначе намертво заблокирует коннект пула БД

    def get_slot_capacity(self, slot_id: int) -> int: ...

    # Дата ближайшего занятия, на которое в момент `after` ещё открыта запись:
    # граница — фактическое начало (с учётом переноса) минус BOOKING_CUTOFF
    def get_next_lesson_date(self, slot_id: int, after: datetime) -> date: ...

    def get_slot_trial_info(self, slot_id: int) -> SlotTrialInfo: ...


@dataclass(frozen=True, slots=True)
class EventHold:
    # Результат удержания мест под платную бронь: что подтверждать
    # и сколько стоит (снимок цены × мест под локом события)
    registration_id: int
    amount_kopecks: int
    description: str


@dataclass(frozen=True, slots=True)
class EventBookingContacts:
    parent_name: str
    email: str
    phone: str


@dataclass(frozen=True, slots=True)
class EventBookingSummary:
    # ПОЧЕМУ без имён и контактов: уходит на публичный экран результата оплаты
    event_id: int
    title: str
    starts_at: datetime
    attendees_count: int


class EventPriceChangedError(Exception):
    # Цена события сменилась между формой и записью (платное ↔ бесплатное)
    def __init__(self, event_id: int) -> None:
        super().__init__(f"Цена события {event_id} изменилась — обновите страницу.")
        self.event_id = event_id


@runtime_checkable
class EventBookingPort(Protocol):
    # !!!: вызывается под select_for_update транзакции — порядок захвата
    # «транзакция → событие → бронь». Реализация обязана работать без сетевого I/O

    # PENDING_PAYMENT → CONFIRMED; False — бронь уже не ждёт оплаты
    def confirm_paid(self, registration_id: int) -> bool: ...

    # PENDING_PAYMENT → CANCELED с возвратом мест; notify — письмо «бронь снята»
    def release_unpaid(self, registration_id: int, *, notify: bool) -> bool: ...

    # CONFIRMED → CANCELED с возвратом мест; False — уже отменена
    def cancel_paid(self, registration_id: int) -> bool: ...

    def get_contacts(self, registration_id: int) -> EventBookingContacts: ...

    def get_summary(self, registration_id: int) -> EventBookingSummary: ...


class ScheduleIntegrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BILLING_")

    schedule_port_class: str = "apps.schedule.ports.DjangoSchedulePort"
    event_port_class: str = "apps.events.ports.DjangoEventBookingPort"


def resolve_schedule_port() -> SchedulePort:
    # ПОЧЕМУ: Точка внедрения зависимости.
    # Вызывать строго на границе приложения (view/task),
    # запрещено вызывать внутри бизнес-сервисов для сохранения чистоты архитектуры
    dotted = ScheduleIntegrationSettings().schedule_port_class
    port_class = import_string(dotted)
    port = port_class()
    if not isinstance(port, SchedulePort):
        raise TypeError(f"{dotted} не реализует протокол SchedulePort.")
    return port


def resolve_event_port() -> EventBookingPort:
    # ПОЧЕМУ: как resolve_schedule_port — только на границе (view/task/admin)
    dotted = ScheduleIntegrationSettings().event_port_class
    port = import_string(dotted)()
    if not isinstance(port, EventBookingPort):
        raise TypeError(f"{dotted} не реализует протокол EventBookingPort.")
    return port
