from __future__ import annotations

from dataclasses import asdict
from typing import Final, cast

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    PolymorphicProxySerializer,
    extend_schema,
)
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.ports import EventPriceChangedError
from apps.billing.serializers import CheckoutResponseSerializer
from apps.billing.views import (
    EventPriceChangedConflict,
    decoy_event_checkout_response,
    event_checkout_response,
    require_idempotency_key,
)
from apps.events.serializers import (
    EventRegistrationCreateSerializer,
    RegistrationAcceptedSerializer,
)
from apps.events.services import (
    build_submission,
    hold_paid_registration,
    is_honeypot,
    is_paid_event,
    process_registration_submission,
)
from apps.events.throttling import EventRegistrationIPThrottle
from apps.users.consent import ConsentSource

_ACCEPTED_BODY: Final[dict[str, str]] = {"status": "accepted"}


class EventRegistrationCreateView(APIView):
    # ПОЧЕМУ: аутентификация дефолтная (JWT), но НЕ обязательная — ивент
    # регистрируется анонимно, а токен, если есть, привязывает бронь к ЛК
    permission_classes = (AllowAny,)
    throttle_classes = (EventRegistrationIPThrottle,)

    @extend_schema(
        tags=["forms"],
        operation_id="public_event_register",
        request=EventRegistrationCreateSerializer,
        parameters=[
            OpenApiParameter(
                name="X-Idempotency-Key",
                location=OpenApiParameter.HEADER,
                type=OpenApiTypes.UUID,
                required=False,
                description="Только для платного события (обязателен): UUID v4, "
                "один на заполненную форму. Повтор с тем же ключом и телом "
                "вернёт тот же платёж.",
            )
        ],
        responses={
            status.HTTP_201_CREATED: PolymorphicProxySerializer(
                component_name="EventRegistrationResult",
                serializers=[
                    RegistrationAcceptedSerializer,
                    CheckoutResponseSerializer,
                ],
                resource_type_field_name=None,
            )
        },
        summary="Гостевая регистрация на событие",
        description="Бесплатное событие → 201 {status: accepted}. Платное → "
        "бронь на время оплаты и платёж: 201 {transaction_id, status: "
        "PENDING_PAYMENT, payment_url, expires_at}, фронт уводит на payment_url.",
    )
    def post(self, request: Request, event_id: int) -> Response:
        serializer = EventRegistrationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = cast("dict[str, object]", serializer.validated_data)
        parent = request.user if request.user.is_authenticated else None
        consent = ConsentSource.from_request(request)

        if not is_paid_event(event_id):
            try:
                process_registration_submission(event_id, data, consent, parent=parent)
            except EventPriceChangedError as exc:
                raise EventPriceChangedConflict() from exc
            # ПОЧЕМУ: ответ одинаков для реальной регистрации и honeypot-дропа,
            # чтобы бот не отличил ловушку
            return Response(_ACCEPTED_BODY, status=status.HTTP_201_CREATED)

        # ПОЧЕМУ ключ до ловушки: без ключа настоящий путь отвечает 422 —
        # иначе бот отличил бы ловушку по ответу
        idempotency_key = require_idempotency_key(request)
        if is_honeypot(event_id, data):
            return decoy_event_checkout_response()

        submission = build_submission(data)
        return event_checkout_response(
            request,
            idempotency_key=idempotency_key,
            fingerprint_payload=asdict(submission),
            phone=submission.phone,
            parent_id=parent.pk if parent is not None else None,
            hold=lambda: hold_paid_registration(event_id, submission, parent, consent),
        )
