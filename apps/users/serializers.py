from __future__ import annotations

from rest_framework import serializers


class OTPRequestSerializer(serializers.Serializer[None]):
    """
    Валидация ФОРМАТА запроса на отправку OTP-кода — и только формата.

    Нормализация email (lower/strip) намеренно отсутствует: канонизация —
    доменный инвариант, от которого зависят advisory-lock и уникальность
    Parent, и живёт она в сервисном слое (services._normalize_email).
    Сериализатор — тонкая UI-обёртка; сервис не доверяет вызывающему коду
    (Defense in Depth: management-команда или RPC в обход DRF получают
    ту же канонизацию).
    """

    email = serializers.EmailField(
        help_text="Email адрес для получения кода",
    )


class OTPVerifySerializer(serializers.Serializer[None]):
    """
    Валидация ФОРМАТА запроса на верификацию OTP-кода.
    Нормализация email — в сервисном слое (см. OTPRequestSerializer).
    """

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
