from __future__ import annotations

from django.apps import AppConfig


class MeConfig(AppConfig):
    # ПОЧЕМУ: авторизованный composition-слой (BFF) личного кабинета.
    # Собственных моделей нет; как и public_api, читает чужие домены,
    # но строго в границах request.user
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.me"
    verbose_name = "Личный кабинет"
