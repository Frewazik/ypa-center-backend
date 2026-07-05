from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.constants import OTP_CODE_TTL_SECONDS, OTP_COOLDOWN_SECONDS
from apps.users.serializers import OTPRequestSerializer, OTPVerifySerializer
from apps.users.throttling import (
    OTPRequestPerEmailThrottle,
    OTPRequestPerIPThrottle,
    OTPVerifyPerIPThrottle,
)
from apps.users.services import (
    OTPBruteForceError,
    OTPCooldownError,
    OTPExpiredError,
    OTPInvalidError,
    OTPNotFoundError,
    request_otp,
    verify_otp,
)


class OTPRequestView(APIView):
    """
    POST /api/v1/auth/otp/request

    Принимает email и инициирует отправку OTP-кода.
    Всегда возвращает 202 — анти-энумерация (нельзя понять, есть ли email в БД).
    429 с Retry-After при cooldown (api-core-contracts.md §0.2).

    Троттлинг стоит ЗДЕСЬ, перед сервисным слоем: cooldown в сервисе защищает
    один email, а глобальный флуд уникальными адресами (CWE-400, спам через
    брокер) отсекается до любых обращений к БД и Taskiq — см. throttling.py.
    """

    permission_classes = [AllowAny]
    throttle_classes = [OTPRequestPerIPThrottle, OTPRequestPerEmailThrottle]

    @extend_schema(
        request=OTPRequestSerializer,
        responses={
            202: OpenApiResponse(description="Код отправлен на email"),
            # DRF serializer.is_valid(raise_exception=True) выбрасывает ValidationError,
            # который стандартный exception handler DRF преобразует в 400, а не 422.
            # Документация обязана отражать реальное поведение фреймворка.
            400: OpenApiResponse(description="Ошибка валидации формата email"),
            429: OpenApiResponse(description="Cooldown: повторный запрос слишком рано"),
        },
        summary="Запрос OTP-кода",
        tags=["auth"],
    )
    def post(self, request: Request) -> Response:
        serializer = OTPRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email: str = serializer.validated_data["email"]

        try:
            request_otp(email)
        except OTPCooldownError as exc:
            # Retry-After — фактический остаток cooldown из исключения,
            # а не константа: бэкенд авторитетен по времени
            # (api-core-contracts.md §1, «Cooldown-таймер клиенту»).
            response = Response(
                {
                    "code": "RATE_LIMITED",
                    "detail": (
                        f"Повторный запрос кода будет доступен "
                        f"через {exc.retry_after} с."
                    ),
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
            response["Retry-After"] = str(exc.retry_after)
            return response

        return Response(
            {
                "status": "sent",
                "resend_available_in": OTP_COOLDOWN_SECONDS,
                "code_ttl": OTP_CODE_TTL_SECONDS,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class OTPVerifyView(APIView):
    """
    POST /api/v1/auth/otp/verify

    Проверяет OTP-код и при успехе возвращает пару JWT-токенов.

    Per-IP троттлинг: attempts_count ограничивает перебор ОДНОГО кода,
    спрей по множеству ящиков с одного IP отсекается здесь (throttling.py).
    """

    permission_classes = [AllowAny]
    throttle_classes = [OTPVerifyPerIPThrottle]

    @extend_schema(
        request=OTPVerifySerializer,
        responses={
            200: OpenApiResponse(description="Токены выданы"),
            400: OpenApiResponse(description="Ошибка валидации формата полей"),
            401: OpenApiResponse(description="Неверный или истёкший код"),
            429: OpenApiResponse(description="Превышен лимит попыток"),
        },
        summary="Верификация OTP-кода",
        tags=["auth"],
    )
    def post(self, request: Request) -> Response:
        serializer = OTPVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email: str = serializer.validated_data["email"]
        code: str = serializer.validated_data["code"]

        try:
            tokens = verify_otp(email=email, code=code)
        except OTPBruteForceError:
            return Response(
                {
                    "code": "RATE_LIMITED",
                    "detail": "Превышен лимит попыток ввода кода.",
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        except (OTPNotFoundError, OTPInvalidError, OTPExpiredError):
            # CWE-204: все три исключения дают одинаковый ответ.
            # Разделение OTPExpiredError в отдельный блок сливало метаданные:
            # атакующий мог узнать, что жертва запрашивала код 5 минут назад.
            # Сюда же попадает деактивированный аккаунт (сервис маскирует его
            # под OTPInvalidError) — статус аккаунта не раскрывается.
            # Zero-knowledge принцип: клиент получает только факт неуспеха.
            return Response(
                {"code": "OTP_INVALID", "detail": "Неверный или истёкший код."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        return Response(tokens, status=status.HTTP_200_OK)
