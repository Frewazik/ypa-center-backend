# ПОЧЕМУ: жесткая изоляция доменов. Billing ничего не знает про ORM apps.schedule,
# связывание инжектится через BILLING_SCHEDULE_PORT_CLASS

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from django.utils.module_loading import import_string
from pydantic_settings import BaseSettings, SettingsConfigDict


class UnknownSlotError(Exception):
    def __init__(self, slot_id: int) -> None:
        super().__init__(f"Слот {slot_id} не найден в расписании.")
        self.slot_id = slot_id


@runtime_checkable
class SchedulePort(Protocol):
    # !!!: Вызывается внутри транзакции под advisory-локами.
    # Реализация ОБЯЗАНА работать без сетевого I/O,
    # иначе намертво заблокирует коннект пула БД

    def get_slot_capacity(self, slot_id: int) -> int: ...

    def get_next_lesson_date(self, slot_id: int, on_or_after: date) -> date: ...


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
