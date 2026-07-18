from __future__ import annotations

from django.apps import AppConfig


class PublicApiConfig(AppConfig):
    # ПОЧЕМУ: composition-слой (BFF) поверх доменов. Собственных моделей нет,
    # только read-only проекции чужих доменов для публичной витрины —
    # единственное место, где разрешены кросс-доменные импорты моделей
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.public_api"
    verbose_name = "Публичная витрина"

    def ready(self) -> None:
        from apps.public_api import signals  # noqa: F401
