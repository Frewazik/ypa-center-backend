from __future__ import annotations

from django.contrib.auth import get_user_model
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import (
    TokenRefreshSerializer as BaseTokenRefreshSerializer,
)


class OTPRequestSerializer(serializers.Serializer[None]):
    # ПОЧЕМУ: нормализация email (канонизация) намеренно вынесена в сервисный слой.
    email = serializers.EmailField(
        help_text="Email адрес для получения кода",
    )


class OTPVerifySerializer(serializers.Serializer[None]):
    email = serializers.EmailField(
        help_text="Email адрес, на который был отправлен код",
    )
    code = serializers.RegexField(
        regex=r"^\d{6}$",
        help_text="Шестизначный числовой код из письма",
        error_messages={
            "invalid": "Код должен содержать ровно 6 цифр.",
        },
    )


class LogoutSerializer(serializers.Serializer[None]):
    refresh = serializers.CharField(
        help_text="Refresh-токен для аннулирования",
    )


class TokenRefreshSerializer(BaseTokenRefreshSerializer):
    default_error_messages = {
        "no_active_account": "Аккаунт не найден или отключён.",
    }

    def validate(self, attrs: dict[str, object]) -> dict[str, str]:
        try:
            return super().validate(attrs)
        except get_user_model().DoesNotExist as exc:
            # ПОЧЕМУ: simplejwt 5.5.1 делает objects.get() без обработки — родитель,
            # удалённый через админку, давал 500. Ответ как у деактивированного,
            # чтобы снаружи не различались «удалён» и «отключён»
            raise AuthenticationFailed(
                self.error_messages["no_active_account"], "no_active_account"
            ) from exc
