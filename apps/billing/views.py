from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping

from asgiref.sync import async_to_sync
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import (
    APIException,
    NotFound,
    PermissionDenied,
    ValidationError,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.permissions import YookassaIPAllowlist
from apps.billing.ports import resolve_schedule_port
from apps.billing.serializers import (
    CheckoutResponseSerializer,
    CheckoutSubscriptionSerializer,
    YookassaWebhookSerializer,
)
from apps.billing.services import (
    DuplicateEnrollmentError,
    IdempotencyKeyReusedError,
    NoAvailableSeatsError,
    PaymentInProgressError,
    PlanNotFoundError,
    PlanSlotsMismatchError,
    SlotNotFoundError,
    StudentNotOwnedError,
    create_payment,
)
from apps.billing.tasks import verify_and_process_payment

_IDEMPOTENCY_HEADER = "X-Idempotency-Key"
_PAYMENT_EVENTS = frozenset(
    ("payment.succeeded", "payment.canceled", "payment.waiting_for_capture")
)


class IdempotencyKeyConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Idempotency-Key уже использован с другим телом запроса."
    default_code = "IDEMPOTENCY_KEY_REUSED"


class PaymentProcessingConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Платёж по этому ключу уже обрабатывается. Повторите запрос позже."
    default_code = "PAYMENT_IN_PROGRESS"


class NoSeatsConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "В выбранном слоте не осталось свободных мест."
    default_code = "NO_AVAILABLE_SEATS"


class EnrollmentConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Ребёнок уже записан или забронирован в этот слот."
    default_code = "STUDENT_ALREADY_ENROLLED"


class CheckoutSubscriptionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=CheckoutSubscriptionSerializer,
        responses={status.HTTP_201_CREATED: CheckoutResponseSerializer},
        description="Идемпотентное создание платежа за абонемент. "
        f"Заголовок {_IDEMPOTENCY_HEADER} (UUID v4) обязателен. "
        "Родитель определяется по сессии — parent_id в теле не принимается.",
    )
    def post(self, request: Request) -> Response:
        idempotency_key = self._require_idempotency_key(request)
        parent_id = self._resolve_parent_id(request)

        serializer = CheckoutSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        fingerprint = _request_fingerprint(request.path, data, parent_id)

        try:
            result = create_payment(
                parent_id=parent_id,
                plan_id=data["plan_id"],
                student_id=data["student_id"],
                slot_ids=data["slot_ids"],
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                schedule_port=resolve_schedule_port(),
                use_deposit=data["use_deposit"],
            )
        except PlanNotFoundError as exc:
            raise NotFound(detail=str(exc)) from exc
        except SlotNotFoundError as exc:
            raise NotFound(detail=str(exc)) from exc
        except PlanSlotsMismatchError as exc:
            raise ValidationError({"slot_ids": str(exc)}) from exc
        except StudentNotOwnedError as exc:
            raise PermissionDenied(detail=str(exc), code="FORBIDDEN_RESOURCE") from exc
        except NoAvailableSeatsError as exc:
            raise NoSeatsConflict(detail=str(exc)) from exc
        except DuplicateEnrollmentError as exc:
            raise EnrollmentConflict(detail=str(exc)) from exc
        except IdempotencyKeyReusedError as exc:
            raise IdempotencyKeyConflict() from exc
        except PaymentInProgressError:
            # ПОЧЕМУ: стандартный DRF APIException не позволяет передать кастомные заголовки
            # формируем ответ вручную для возврата Retry-After
            return _problem_response(
                code=PaymentProcessingConflict.default_code,
                title="Платёж уже обрабатывается",
                detail=str(PaymentProcessingConflict.default_detail),
                status_code=status.HTTP_409_CONFLICT,
                instance=request.path,
                headers={"Retry-After": "5"},
            )

        response = CheckoutResponseSerializer(
            {
                "transaction_id": result.transaction_id,
                "status": result.status,
                "payment_url": result.payment_url,
                "expires_at": result.expires_at,
            }
        )
        return Response(response.data, status=status.HTTP_201_CREATED)

    def _require_idempotency_key(self, request: Request) -> str:
        raw_key = request.headers.get(_IDEMPOTENCY_HEADER)
        if not raw_key:
            raise ValidationError(
                {_IDEMPOTENCY_HEADER: "Заголовок обязателен для этой операции."},
                code="IDEMPOTENCY_KEY_REQUIRED",
            )
        try:
            return str(uuid.UUID(raw_key))
        except ValueError as exc:
            raise ValidationError(
                {_IDEMPOTENCY_HEADER: "Значение должно быть валидным UUID."},
                code="IDEMPOTENCY_KEY_MALFORMED",
            ) from exc

    def _resolve_parent_id(self, request: Request) -> int:
        # !!!: ID родителя берется строго из контекста авторизации
        # чтение из тела запроса запрещено для защиты от IDOR
        parent = getattr(request.user, "parent", None)
        if parent is None:
            raise PermissionDenied(
                detail="У учётной записи нет профиля родителя.",
                code="PARENT_PROFILE_REQUIRED",
            )
        return int(parent.pk)


class YookassaWebhookView(APIView):
    # ПОЧЕМУ: тело вебхука не является доверенным источником истины
    # извлекаем только object.id как триггер, реальный статус запрашивает воркер

    authentication_classes = []
    permission_classes = [YookassaIPAllowlist]

    @extend_schema(
        request=YookassaWebhookSerializer,
        responses={status.HTTP_200_OK: None},
        description="Вебхук ЮКассы. Быстро ставит верификацию платежа в очередь.",
    )
    def post(self, request: Request) -> Response:
        # ПОЧЕМУ: фильтруем чужие события до валидации сериализатором
        # иначе не прошедший regex ID даст 400/422 и спровоцирует ретрай-шторм от провайдера
        raw_event = (
            request.data.get("event") if isinstance(request.data, dict) else None
        )
        if raw_event not in _PAYMENT_EVENTS:
            return Response({"status": "ignored"}, status=status.HTTP_200_OK)

        serializer = YookassaWebhookSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment_id: str = serializer.validated_data["object"]["id"]

        # ПОЧЕМУ: Taskiq-брокер использует async/await
        # вызов из синхронного Django-view требует обертки async_to_sync
        async_to_sync(verify_and_process_payment.kiq)(payment_id)
        return Response({"status": "accepted"}, status=status.HTTP_200_OK)


_PROBLEM_TYPE_BASE = "https://api.ypa-center.ru/problems"


def _problem_response(
    *,
    code: str,
    title: str,
    detail: str,
    status_code: int,
    instance: str,
    headers: dict[str, str] | None = None,
) -> Response:
    # ПОЧЕМУ: DRF из коробки не умеет в стандарт RFC 9457 с кастомными заголовками
    # собираем тело проблемы руками
    body = {
        "type": f"{_PROBLEM_TYPE_BASE}/{code.lower().replace('_', '-')}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": instance,
        "code": code,
    }
    return Response(
        body,
        status=status_code,
        headers=headers,
        content_type="application/problem+json",
    )


def _request_fingerprint(
    path: str, payload: Mapping[str, object], parent_id: int
) -> str:
    # ПОЧЕМУ: добавление parent_id в соль изолирует ключи идемпотентности по аккаунту
    # это математически исключает кросс-тенантные коллизии
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(f"{path}|parent:{parent_id}|{canonical}".encode()).hexdigest()
