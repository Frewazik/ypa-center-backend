from __future__ import annotations

from typing import Final, cast

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.events.serializers import (
    EventRegistrationCreateSerializer,
    RegistrationAcceptedSerializer,
)
from apps.events.services import process_registration_submission
from apps.events.throttling import EventRegistrationIPThrottle

_ACCEPTED_BODY: Final[dict[str, str]] = {"status": "accepted"}


class EventRegistrationCreateView(APIView):
    # ПОЧЕМУ: аутентификация дефолтная (JWT), но НЕ обязательная — ивент
    # регистрируется анонимно, а токен, если есть, привязывает бронь к ЛК
    permission_classes = (AllowAny,)
    throttle_classes = (EventRegistrationIPThrottle,)

    @extend_schema(
        operation_id="public_event_register",
        request=EventRegistrationCreateSerializer,
        responses={status.HTTP_201_CREATED: RegistrationAcceptedSerializer},
        summary="Гостевая регистрация на событие",
    )
    def post(self, request: Request, event_id: int) -> Response:
        serializer = EventRegistrationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        parent = request.user if request.user.is_authenticated else None
        process_registration_submission(
            event_id,
            cast("dict[str, object]", serializer.validated_data),
            parent=parent,
        )
        # ПОЧЕМУ: ответ одинаков для реальной регистрации и honeypot-дропа,
        # чтобы бот не отличил ловушку
        return Response(_ACCEPTED_BODY, status=status.HTTP_201_CREATED)
