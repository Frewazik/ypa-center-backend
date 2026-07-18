# Точка входа воркера и шедулера Taskiq:
#   taskiq worker config.worker:broker
#   taskiq scheduler config.worker:scheduler
# ПОЧЕМУ: процесс воркера живёт вне Django — реестр приложений обязан быть
# инициализирован до импорта модулей с задачами, иначе AppRegistryNotReady.
from __future__ import annotations

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

# ПОЧЕМУ: регистрация задач — побочный эффект импорта; без этих импортов
# воркер примет сообщение и не найдёт обработчик.
import apps.billing.tasks  # noqa: E402, F401
import apps.events.tasks  # noqa: E402, F401
import apps.journal.tasks  # noqa: E402, F401
import apps.public_forms.tasks  # noqa: E402, F401
import apps.users.tasks  # noqa: E402, F401
from config.tkq import broker, scheduler  # noqa: E402

__all__ = ["broker", "scheduler"]
