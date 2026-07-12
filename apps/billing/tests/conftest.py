"""Тестовое окружение Billing: переменная ставится до импорта `config.tkq`, брокер собирается InMemory."""

from __future__ import annotations

import os

os.environ.setdefault("TASKIQ_USE_INMEMORY_BROKER", "true")
