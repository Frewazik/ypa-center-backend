from __future__ import annotations

import datetime

from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.schedule.serializers import WeekGridResponseSerializer
from apps.schedule.services import build_week_grid, normalize_week_start


class PublicScheduleView(APIView):
    permission_classes = (AllowAny,)
    authentication_classes = ()

    @extend_schema(
        operation_id="public_schedule_week",
        summary="Недельная сетка расписания",
        parameters=[
            OpenApiParameter(
                name="week_start",
                type=OpenApiTypes.DATE,
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    "Понедельник запрашиваемой недели (ISO 8601, YYYY-MM-DD). "
                    "По умолчанию — текущая неделя; не-понедельник "
                    "нормализуется к началу своей недели."
                ),
            )
        ],
        responses=WeekGridResponseSerializer,
    )
    def get(self, request: Request) -> Response:
        week_start = _resolve_week_start(request.query_params.get("week_start"))
        slots = build_week_grid(week_start)
        payload = {
            "week_start": week_start,
            "week_end": week_start + datetime.timedelta(days=6),
            "slots": slots,
        }
        return Response(WeekGridResponseSerializer(payload).data)


def _resolve_week_start(raw: str | None) -> datetime.date:
    # ПОЧЕМУ: Ошибки парсинга бросают DRF ValidationError.
    # Глобальный обработчик RFC 9457 сам превратит его в 422 ответ по контракту
    if not raw:
        return normalize_week_start(timezone.localdate())
    try:
        parsed = datetime.date.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError(
            {"week_start": "Ожидается дата в формате ISO 8601 (YYYY-MM-DD)."},
            code="VALIDATION_ERROR",
        ) from exc
    return normalize_week_start(parsed)
