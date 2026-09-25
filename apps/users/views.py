from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import exceptions, serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from apps.users.constants import OTP_CODE_TTL_SECONDS, OTP_COOLDOWN_SECONDS
from apps.users.serializers import (
    LogoutSerializer,
    OTPRequestSerializer,
    OTPVerifySerializer,
)
from apps.users.throttling import (
    AuthLogoutThrottle,
    AuthTokenRefreshThrottle,
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
    # ПОЧЕМУ: токен на входе не нужен и вреден — с валидным токеном
    # запрос считался бы «не анонимным» и мог обходить IP-лимит
    authentication_classes = ()
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
    authentication_classes = ()
    permission_classes = [AllowAny]
    throttle_classes = [OTPVerifyPerIPThrottle]

    @extend_schema(
        request=OTPVerifySerializer,
        responses={
            200: inline_serializer(
                "OTPVerifyResponse",
                fields={
                    "access": serializers.CharField(),
                    "refresh": serializers.CharField(),
                    "profile_completed": serializers.BooleanField(
                        help_text=(
                            "false — показать анкету (PATCH /me/profile/); "
                            "до её заполнения ЛК и покупки отвечают 403"
                        ),
                    ),
                },
            ),
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
            {
                "access": tokens.access,
                "refresh": tokens.refresh,
                "profile_completed": tokens.profile_completed,
            },
            status=status.HTTP_200_OK,
        )


@extend_schema(
    responses={
        200: inline_serializer(
            "TokenRefreshResponse",
            fields={
                "access": serializers.CharField(),
                "refresh": serializers.CharField(),
            },
        ),
        401: OpenApiResponse(description="Невалидный или истёкший refresh-токен"),
    },
    summary="Обновление access-токена",
    tags=["auth"],
)
class AuthTokenRefreshView(TokenRefreshView):
    permission_classes = ()
    throttle_classes = [AuthTokenRefreshThrottle]


class LogoutView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthLogoutThrottle]

    @extend_schema(
        request=LogoutSerializer,
        responses={
            205: OpenApiResponse(description="Успешный выход, токен аннулирован"),
            400: OpenApiResponse(description="Ошибка валидации формата полей"),
            401: OpenApiResponse(description="Невалидный или истёкший токен"),
        },
        summary="Выход из системы (аннулирование refresh-токена)",
        tags=["auth"],
    )
    def post(self, request: Request) -> Response:
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            token = RefreshToken(serializer.validated_data["refresh"])
            token.blacklist()
        except TokenError:
            raise InvalidToken("Неверный или истёкший refresh-токен.")

        return Response(status=status.HTTP_205_RESET_CONTENT)
