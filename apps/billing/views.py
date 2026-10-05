from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from typing import cast

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.exceptions import (
    APIException,
    NotFound,
    PermissionDenied,
    ValidationError,
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.adapters import YookassaHttpGateway, event_return_url
from apps.billing.permissions import YookassaIPAllowlist
from apps.billing.ports import (
    EventHold,
    EventPriceChangedError,
    resolve_event_port,
    resolve_schedule_port,
)
from apps.billing.serializers import (
    CheckoutResponseSerializer,
    CheckoutSubscriptionSerializer,
    CheckoutTransactionSerializer,
    CheckoutTrialSerializer,
    EventCheckoutTransactionSerializer,
    YookassaWebhookSerializer,
)
from apps.billing.services import (
    CheckoutResult,
    CheckoutTransactionNotFoundError,
    DuplicateEnrollmentError,
    IdempotencyKeyReusedError,
    NoAvailableSeatsError,
    PaymentGatewayUnavailableError,
    PaymentInProgressError,
    PlanNotFoundError,
    PlanSlotsMismatchError,
    PlanUnavailableError,
    SlotNotFoundError,
    StudentNotOwnedError,
    TrialDateUnavailableError,
    TrialLimitExceededError,
    create_event_payment,
    create_payment,
    create_trial_payment,
    decoy_event_checkout,
    get_checkout_outcome,
    get_event_checkout_outcome,
)
from apps.billing.tasks import verify_and_process_payment
from apps.core.queue import kiq_sync
from apps.core.throttling import ClientIPRateThrottle
from apps.users.models import Parent
from apps.users.permissions import IsProfileCompleted

_IDEMPOTENCY_HEADER = "X-Idempotency-Key"
# Поле для заголовка в Swagger — без него чекаут оттуда не вызвать
_IDEMPOTENCY_PARAMETER = OpenApiParameter(
    name=_IDEMPOTENCY_HEADER,
    location=OpenApiParameter.HEADER,
    type=OpenApiTypes.UUID,
    required=True,
    description="UUID v4, новый на каждую покупку. Повтор с тем же ключом "
    "и тем же телом вернёт тот же ответ, с другим телом — 409.",
)
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


class PlanUnavailableConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Тариф снят с продажи. Обновите список тарифов."
    default_code = "PLAN_UNAVAILABLE"


class TrialLimitConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "У ребёнка уже есть пробное занятие по этому кружку."
    default_code = "TRIAL_LIMIT_EXCEEDED"


class EventPriceChangedConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Цена события изменилась. Обновите страницу."
    default_code = "EVENT_PRICE_CHANGED"


def require_idempotency_key(request: Request) -> str:
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


def checkout_response(result: CheckoutResult) -> Response:
    response = CheckoutResponseSerializer(
        {
            "transaction_id": result.transaction_id,
            "status": result.status,
            "payment_url": result.payment_url,
            "expires_at": result.expires_at,
        }
    )
    return Response(response.data, status=status.HTTP_201_CREATED)


def payment_in_progress_response(request: Request) -> Response:
    # ПОЧЕМУ: стандартный DRF APIException не позволяет передать кастомные
    # заголовки — формируем ответ вручную для возврата Retry-After
    return _problem_response(
        code=PaymentProcessingConflict.default_code,
        title="Платёж уже обрабатывается",
        detail=str(PaymentProcessingConflict.default_detail),
        status_code=status.HTTP_409_CONFLICT,
        instance=request.path,
        headers={"Retry-After": "5"},
    )


def gateway_unavailable_response(request: Request) -> Response:
    # ПОЧЕМУ: заказ аннулирован, резервация ключа снята — повтор с тем же
    # Idempotency-Key безопасен, поэтому 503 с Retry-After, а не 500
    return _problem_response(
        code="PAYMENT_GATEWAY_UNAVAILABLE",
        title="Платёжный шлюз недоступен",
        detail="Не удалось создать платёж. Повторите запрос позже.",
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        instance=request.path,
        headers={"Retry-After": "30"},
    )


def event_checkout_response(
    request: Request,
    *,
    idempotency_key: str,
    fingerprint_payload: Mapping[str, object],
    phone: str,
    parent_id: int | None,
    hold: Callable[[], EventHold],
) -> Response:
    # Платная ветка POST /public/events/{id}/register/: ручка формы остаётся
    # в events, а деньги и их ошибки — здесь, как у остальных чекаутов
    fingerprint = _request_fingerprint(
        request.path, fingerprint_payload, salt=f"phone:{phone}"
    )
    try:
        result = create_event_payment(
            hold=hold,
            parent_id=parent_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            gateway=YookassaHttpGateway(),
            event_port=resolve_event_port(),
        )
    except EventPriceChangedError as exc:
        raise EventPriceChangedConflict() from exc
    except IdempotencyKeyReusedError as exc:
        raise IdempotencyKeyConflict() from exc
    except PaymentInProgressError:
        return payment_in_progress_response(request)
    except PaymentGatewayUnavailableError:
        return gateway_unavailable_response(request)
    return checkout_response(result)


def decoy_event_checkout_response() -> Response:
    return checkout_response(decoy_event_checkout(event_return_url))


class _CheckoutView(APIView):
    # ПОЧЕМУ: список задан явно (не из settings), поэтому анкету проверяем
    # тут же — покупка до заполнения анкеты закрыта
    permission_classes = [IsAuthenticated, IsProfileCompleted]

    def _require_idempotency_key(self, request: Request) -> str:
        return require_idempotency_key(request)

    def _resolve_parent_id(self, request: Request) -> int:
        # !!!: ID родителя берется строго из контекста авторизации
        # чтение из тела запроса запрещено для защиты от IDOR.
        # AUTH_USER_MODEL == users.Parent, поэтому request.user и есть родитель;
        # IsAuthenticated гарантирует, что это не AnonymousUser
        return int(cast(Parent, request.user).pk)

    def _checkout_response(self, result: CheckoutResult) -> Response:
        return checkout_response(result)

    def _payment_in_progress_response(self, request: Request) -> Response:
        return payment_in_progress_response(request)

    def _gateway_unavailable_response(self, request: Request) -> Response:
        return gateway_unavailable_response(request)


class CheckoutSubscriptionView(_CheckoutView):
    @extend_schema(
        request=CheckoutSubscriptionSerializer,
        responses={status.HTTP_201_CREATED: CheckoutResponseSerializer},
        description="Идемпотентное создание платежа за абонемент. "
        f"Заголовок {_IDEMPOTENCY_HEADER} (UUID v4) обязателен. "
        "Родитель определяется по сессии — parent_id в теле не принимается.",
        parameters=[_IDEMPOTENCY_PARAMETER],
        tags=["checkout"],
    )
    def post(self, request: Request) -> Response:
        idempotency_key = self._require_idempotency_key(request)
        parent_id = self._resolve_parent_id(request)

        serializer = CheckoutSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # ПОЧЕМУ sorted: сервис слоты сортирует, значит [101, 102] и [102, 101] —
        # один и тот же заказ; иначе повтор с тем же ключом ловит ложный 409
        fingerprint = _request_fingerprint(
            request.path,
            {**data, "slot_ids": sorted(data["slot_ids"])},
            salt=f"parent:{parent_id}",
        )

        try:
            result = create_payment(
                parent_id=parent_id,
                plan_id=data["plan_id"],
                student_id=data["student_id"],
                slot_ids=data["slot_ids"],
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                gateway=YookassaHttpGateway(),
                schedule_port=resolve_schedule_port(),
                use_deposit=data["use_deposit"],
            )
        except PlanNotFoundError as exc:
            raise NotFound(detail=str(exc)) from exc
        except PlanUnavailableError as exc:
            raise PlanUnavailableConflict(detail=str(exc)) from exc
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
            return self._payment_in_progress_response(request)
        except PaymentGatewayUnavailableError:
            return self._gateway_unavailable_response(request)

        return self._checkout_response(result)


class CheckoutTrialView(_CheckoutView):
    @extend_schema(
        request=CheckoutTrialSerializer,
        responses={status.HTTP_201_CREATED: CheckoutResponseSerializer},
        description="Идемпотентная запись на пробное занятие. "
        f"Заголовок {_IDEMPOTENCY_HEADER} (UUID v4) обязателен. "
        "Не более одного пробного на ребёнка по кружку. "
        "Бесплатное пробное подтверждается сразу (CONFIRMED).",
        parameters=[_IDEMPOTENCY_PARAMETER],
        tags=["checkout"],
    )
    def post(self, request: Request) -> Response:
        idempotency_key = self._require_idempotency_key(request)
        parent_id = self._resolve_parent_id(request)

        serializer = CheckoutTrialSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        fingerprint = _request_fingerprint(
            request.path,
            {
                "student_id": data["student_id"],
                "schedule_id": data["schedule_id"],
                "trial_date": data["trial_date"].isoformat(),
            },
            salt=f"parent:{parent_id}",
        )

        try:
            result = create_trial_payment(
                parent_id=parent_id,
                student_id=data["student_id"],
                schedule_id=data["schedule_id"],
                trial_date=data["trial_date"],
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                gateway=YookassaHttpGateway(),
                schedule_port=resolve_schedule_port(),
            )
        except SlotNotFoundError as exc:
            raise NotFound(detail=str(exc)) from exc
        except StudentNotOwnedError as exc:
            raise PermissionDenied(detail=str(exc), code="FORBIDDEN_RESOURCE") from exc
        except TrialDateUnavailableError as exc:
            raise ValidationError({"trial_date": str(exc)}) from exc
        except TrialLimitExceededError as exc:
            raise TrialLimitConflict(detail=str(exc)) from exc
        except NoAvailableSeatsError as exc:
            raise NoSeatsConflict(detail=str(exc)) from exc
        except DuplicateEnrollmentError as exc:
            raise EnrollmentConflict(detail=str(exc)) from exc
        except IdempotencyKeyReusedError as exc:
            raise IdempotencyKeyConflict() from exc
        except PaymentInProgressError:
            return self._payment_in_progress_response(request)
        except PaymentGatewayUnavailableError:
            return self._gateway_unavailable_response(request)

        return self._checkout_response(result)


class CheckoutTransactionView(_CheckoutView):
    @extend_schema(
        responses={status.HTTP_200_OK: CheckoutTransactionSerializer},
        description="Итог оплаты для страницы «результат оплаты» (id приходит "
        "в return_url как ?tx=). Фронт опрашивает, пока status = PENDING. "
        "Чужая или несуществующая транзакция — 404.",
        parameters=[
            OpenApiParameter(
                name="transaction_id",
                location=OpenApiParameter.PATH,
                type=OpenApiTypes.UUID,
            )
        ],
        tags=["checkout"],
    )
    def get(self, request: Request, transaction_id: str) -> Response:
        # ПОЧЕМУ str в URL и разбор здесь: битый id отвечает тем же 404
        # в формате RFC 9457, что и чужой, а не HTML-страницей Django
        try:
            tx_id = uuid.UUID(transaction_id)
            outcome = get_checkout_outcome(tx_id, self._resolve_parent_id(request))
        except (ValueError, CheckoutTransactionNotFoundError) as exc:
            raise NotFound(detail="Транзакция не найдена.") from exc
        return Response(CheckoutTransactionSerializer(outcome).data)


class EventPaymentStatusIPThrottle(ClientIPRateThrottle):
    scope = "event_payment_status"


class EventCheckoutTransactionView(APIView):
    # ПОЧЕМУ без входа: гость платит без аккаунта, доказательство — только id
    # из return_url (uuid4, не угадать). Поэтому в ответе нет ПД, а id
    # абонемента или пробного здесь — тот же 404, что и несуществующий
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [EventPaymentStatusIPThrottle]

    @extend_schema(
        responses={status.HTTP_200_OK: EventCheckoutTransactionSerializer},
        description="Итог оплаты события для страницы «результат оплаты» "
        "(return_url приходит с ?tx=…&kind=event). Без входа, без "
        "персональных данных. Фронт опрашивает, пока status = PENDING. "
        "Не транзакция события — 404.",
        parameters=[
            OpenApiParameter(
                name="transaction_id",
                location=OpenApiParameter.PATH,
                type=OpenApiTypes.UUID,
            )
        ],
        tags=["forms"],
        operation_id="public_event_payment_status",
    )
    def get(self, request: Request, transaction_id: str) -> Response:
        try:
            outcome = get_event_checkout_outcome(
                uuid.UUID(transaction_id), event_port=resolve_event_port()
            )
        except (ValueError, CheckoutTransactionNotFoundError) as exc:
            raise NotFound(detail="Транзакция не найдена.") from exc
        return Response(EventCheckoutTransactionSerializer(outcome).data)


class YookassaWebhookView(APIView):
    # ПОЧЕМУ: тело вебхука не является доверенным источником истины
    # извлекаем только object.id как триггер, реальный статус запрашивает воркер

    authentication_classes = []
    permission_classes = [YookassaIPAllowlist]

    @extend_schema(
        request=YookassaWebhookSerializer,
        responses={status.HTTP_200_OK: None},
        description="Вебхук ЮКассы. Быстро ставит верификацию платежа в очередь.",
        tags=["webhooks"],
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

        # ПОЧЕМУ kiq_sync, а не kiq_safely: сбой брокера должен дойти до 500,
        # иначе ЮКасса не повторит вебхук
        kiq_sync(verify_and_process_payment, payment_id)
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


def _request_fingerprint(path: str, payload: Mapping[str, object], *, salt: str) -> str:
    # ПОЧЕМУ соль: изолирует ключи идемпотентности по владельцу — родителю
    # (parent:<id>) или, у гостя без входа, телефону брони (phone:<E.164>)
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(f"{path}|{salt}|{canonical}".encode()).hexdigest()
