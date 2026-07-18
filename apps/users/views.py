from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import exceptions, status
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
    permission_classes = [AllowAny]
    throttle_classes = [OTPRequestPerIPThrottle, OTPRequestPerEmailThrottle]

    @extend_schema(
        request=OTPRequestSerializer,
        responses={
            202: OpenApiResponse(description="Код отправлен на email"),
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
            # ПОЧЕМУ: Throttled уходит в problem_detail_exception_handler,
            # который собирает RFC 9457 тело и заголовок Retry-After
            raise exceptions.Throttled(
                wait=exc.retry_after, detail="Повторный запрос возможен позже."
            )

        return Response(
            {
                "status": "sent",
                "resend_available_in": OTP_COOLDOWN_SECONDS,
                "code_ttl": OTP_CODE_TTL_SECONDS,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class OTPVerifyView(APIView):
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
            # ПОЧЕМУ: разные причины отказа схлопываются в один 401,
            # чтобы закрыть вектор энумерации email
            return Response(
                {"code": "OTP_INVALID", "detail": "Неверный или истёкший код."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        return Response(
            {"access": tokens.access, "refresh": tokens.refresh},
            status=status.HTTP_200_OK,
        )
