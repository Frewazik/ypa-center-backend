from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import exceptions, serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from apps.users.constants import OTP_CODE_TTL_SECONDS, OTP_COOLDOWN_SECONDS
from apps.users.serializers import (
    LogoutSerializer,
    OTPRequestSerializer,
    OTPVerifySerializer,
    TokenRefreshSerializer,
)
from apps.users.throttling import (
    AuthLogoutThrottle,
    AuthTokenRefreshThrottle,
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
    # ПОЧЕМУ: лимит по email — в request_otp по отправленным кодам; счётчик
    # запросов по email запирал бы чужой ящик серией запросов за секунду
    throttle_classes = [OTPRequestPerIPThrottle]

    @extend_schema(
        request=OTPRequestSerializer,
        responses={
            202: OpenApiResponse(description="Код отправлен на email"),
            422: OpenApiResponse(description="Ошибка валидации формата email"),
            429: OpenApiResponse(
                description="RATE_LIMITED + Retry-After: cooldown 60 с, "
                "5 кодов в час на email или лимит по IP"
            ),
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


# ПОЧЕМУ: APIException, а не AuthenticationFailed — у вьюхи нет классов
# аутентификации, и DRF превратил бы такой 401 в 403
class OTPInvalid(exceptions.APIException):
    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "Неверный или истёкший код."
    default_code = "OTP_INVALID"


# ПОЧЕМУ: свой код, а не RATE_LIMITED — ждать бесполезно, код больше не
# примут, фронт должен предложить запросить новый (Retry-After нет)
class OTPAttemptsExceeded(exceptions.APIException):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_detail = "Превышен лимит попыток ввода кода. Запросите новый код."
    default_code = "OTP_ATTEMPTS_EXCEEDED"


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
            422: OpenApiResponse(description="Ошибка валидации формата полей"),
            401: OpenApiResponse(description="OTP_INVALID: неверный или истёкший код"),
            429: OpenApiResponse(
                description="OTP_ATTEMPTS_EXCEEDED: 5 неверных попыток, нужен "
                "новый код (без Retry-After); RATE_LIMITED + Retry-After: "
                "лимит по IP"
            ),
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
        except OTPBruteForceError as exc:
            raise OTPAttemptsExceeded() from exc
        except (OTPNotFoundError, OTPInvalidError, OTPExpiredError) as exc:
            # ПОЧЕМУ: разные причины отказа схлопываются в один 401,
            # чтобы закрыть вектор энумерации email
            raise OTPInvalid() from exc

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
        429: OpenApiResponse(description="Лимит запросов с IP, есть Retry-After"),
    },
    summary="Обновление access-токена",
    tags=["auth"],
)
class AuthTokenRefreshView(TokenRefreshView):
    serializer_class = TokenRefreshSerializer
    permission_classes = ()
    throttle_classes = [AuthTokenRefreshThrottle]


class LogoutView(APIView):
    # ПОЧЕМУ: владение доказывает refresh в теле; с JWT по умолчанию
    # протухший access в заголовке давал 401 до ручки, и refresh не
    # аннулировался — «выйти» на общем компьютере не срабатывало
    authentication_classes = ()
    permission_classes = [AllowAny]
    throttle_classes = [AuthLogoutThrottle]

    def get_authenticate_header(self, request: Request) -> str:
        # ПОЧЕМУ: без классов аутентификации DRF превращает 401 от InvalidToken
        # в 403 — ему нечего положить в WWW-Authenticate. Так же делает TokenViewBase
        return JWTAuthentication().authenticate_header(request)

    @extend_schema(
        request=LogoutSerializer,
        responses={
            205: OpenApiResponse(description="Успешный выход, токен аннулирован"),
            422: OpenApiResponse(description="Ошибка валидации формата полей"),
            401: OpenApiResponse(description="Невалидный или истёкший токен"),
            429: OpenApiResponse(description="Лимит запросов с IP, есть Retry-After"),
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
