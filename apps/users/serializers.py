from __future__ import annotations

from rest_framework import serializers


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
