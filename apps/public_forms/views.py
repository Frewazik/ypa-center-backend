from __future__ import annotations

from typing import Final

from adrf.views import APIView
from drf_spectacular.utils import extend_schema
from ipware import get_client_ip
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from apps.public_forms.serializers import (
    CallbackRequestCreateSerializer,
    FeedbackRequestCreateSerializer,
    SubmissionAcceptedSerializer,
)
from apps.public_forms.services import (
    process_callback_submission,
    process_feedback_submission,
)
from apps.public_forms.throttling import CallbackIPThrottle, FeedbackIPThrottle

_ACCEPTED_BODY: Final[dict[str, str]] = {"status": "accepted"}


class CallbackRequestCreateView(APIView):
    permission_classes = (AllowAny,)
    throttle_classes = (CallbackIPThrottle,)

    @extend_schema(
        request=CallbackRequestCreateSerializer,
        responses={status.HTTP_202_ACCEPTED: SubmissionAcceptedSerializer},
        summary="Заказ обратного звонка",
    )
    async def post(self, request: Request) -> Response:
        serializer = CallbackRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        client_ip, _ = get_client_ip(request)
        await process_callback_submission(serializer.validated_data, client_ip)
        # ПОЧЕМУ: ответ одинаков для реальной заявки и honeypot-дропа,
        # чтобы бот не отличил ловушку
        return Response(_ACCEPTED_BODY, status=status.HTTP_202_ACCEPTED)


class FeedbackRequestCreateView(APIView):
    permission_classes = (AllowAny,)
    throttle_classes = (FeedbackIPThrottle,)

    @extend_schema(
        request=FeedbackRequestCreateSerializer,
        responses={status.HTTP_202_ACCEPTED: SubmissionAcceptedSerializer},
        summary="Обратная связь",
    )
    async def post(self, request: Request) -> Response:
        serializer = FeedbackRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        client_ip, _ = get_client_ip(request)
        await process_feedback_submission(serializer.validated_data, client_ip)
        return Response(_ACCEPTED_BODY, status=status.HTTP_202_ACCEPTED)
