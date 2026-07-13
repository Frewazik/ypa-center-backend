from __future__ import annotations

from typing import Final, cast

from django.urls import path

from adrf.views import APIView
from drf_spectacular.utils import extend_schema
from ipware import get_client_ip
from rest_framework import status
from rest_framework.request import Request
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.public_forms.views import CallbackRequestCreateView, FeedbackRequestCreateView
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

app_name = "public_forms"

urlpatterns = [
    path("callback/", CallbackRequestCreateView.as_view(), name="callback-create"),
    path("feedback/", FeedbackRequestCreateView.as_view(), name="feedback-create"),
]

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
        # REMOTE_ADDR за прокси = IP прокси; берём реальный клиентский IP из X-Forwarded-For
        client_ip, _ = get_client_ip(request)
        await process_callback_submission(
            cast("dict[str, object]", serializer.validated_data), client_ip
        )
        # Ответ одинаков для реальной заявки и honeypot-дропа — бот не отличит ловушку
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
        await process_feedback_submission(
            cast("dict[str, object]", serializer.validated_data), client_ip
        )
        return Response(_ACCEPTED_BODY, status=status.HTTP_202_ACCEPTED)