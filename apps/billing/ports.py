# ПОЧЕМУ: жесткая изоляция доменов. Billing ничего не знает про ORM apps.schedule,
# связывание инжектится через BILLING_SCHEDULE_PORT_CLASS

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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

    def get_next_lesson_date(self, slot_id: int, on_or_after: date) -> date: ...

    def get_slot_trial_info(self, slot_id: int) -> SlotTrialInfo: ...


class ScheduleIntegrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BILLING_")

    schedule_port_class: str = "apps.schedule.ports.DjangoSchedulePort"


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
