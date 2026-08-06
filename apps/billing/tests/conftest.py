from __future__ import annotations

import os

os.environ.setdefault("TASKIQ_USE_INMEMORY_BROKER", "true")

import pytest  # noqa: E402 — импорт после установки env брокера

from apps.schedule.tests.factories import ScheduleFactory  # noqa: E402

# ПОЧЕМУ здесь, а не в модуле теста: слоты нужны и test_billing, и test_trials.
# autouse-фикстура из модуля не видна соседнему — пакетный conftest видят все
_SEEDED_SLOT_IDS = [100, 101, 102, 103, 104, 105, 106, 777]


@pytest.fixture(autouse=True)
def _seed_schedules(db: None) -> None:
    # ПОЧЕМУ: удовлетворяет строгие ограничения ForeignKey на уровне БД
    for slot_id in _SEEDED_SLOT_IDS:
        ScheduleFactory(id=slot_id)
