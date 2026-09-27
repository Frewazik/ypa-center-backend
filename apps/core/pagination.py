from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Final, cast

from django.db.models import QuerySet
from drf_spectacular.utils import OpenApiParameter
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.request import Request
from rest_framework.response import Response

PAGINATION_MAX_LIMIT: Final[int] = 60

# Для extend_schema ручек на APIView: сами они параметры пагинатора не видят
LIMIT_OFFSET_PARAMETERS: Final = [
    OpenApiParameter(
        name="limit",
        type=int,
        required=False,
        description=(
            f"Размер страницы, 1..{PAGINATION_MAX_LIMIT} (больше — урезается). "
            "Без limit — весь список массивом; с limit — конверт "
            "{count, next, previous, results}."
        ),
    ),
    OpenApiParameter(
        name="offset",
        type=int,
        required=False,
        description="Сколько элементов пропустить, по умолчанию 0.",
    ),
]


def parse_int_param(raw: str | None, field: str, *, minimum: int) -> int | None:
    # ПОЧЕМУ isascii: str.isdigit() пропускает «²» и арабские цифры, на
    # которых int() падает — вместо 422 был бы 500
    if raw is None or raw == "":
        return None
    if not (raw.isascii() and raw.isdigit()) or int(raw) < minimum:
        message = (
            "Ожидается целое число больше нуля."
            if minimum == 1
            else f"Ожидается целое число не меньше {minimum}."
        )
        raise ValidationError({field: [message]}, code="VALIDATION_ERROR")
    return int(raw)


class LimitOffsetListPagination(LimitOffsetPagination):
    # ПОЧЕМУ default_limit=None: без ?limit клиент получает полный список
    # массивом, как до пагинации. С ?limit=N&offset=M — конверт
    # {count, next, previous, results} для подгрузки по кнопке/скроллу
    default_limit = None
    max_limit = PAGINATION_MAX_LIMIT

    # ПОЧЕМУ свой разбор: DRF глушит ValueError на ?limit=0/-5/abc и молча
    # откатывается на default_limit=None — клиент просил страницу, а получал
    # весь список в обход max_limit. У нас кривое значение — 422
    def get_limit(self, request: Request) -> int | None:
        limit = parse_int_param(
            request.query_params.get(self.limit_query_param),
            self.limit_query_param,
            minimum=1,
        )
        if limit is None:
            return None
        return min(limit, PAGINATION_MAX_LIMIT)

    def get_offset(self, request: Request) -> int:
        offset = parse_int_param(
            request.query_params.get(self.offset_query_param),
            self.offset_query_param,
            minimum=0,
        )
        return offset or 0


def paginated_response(
    request: Request,
    source: QuerySet[Any] | Sequence[Any],
    to_data: Callable[[list[Any]], Any],
) -> Response:
    """Пагинация для APIView, которые собирают ответ из сервиса.

    source — queryset (страница режется в SQL: COUNT + LIMIT/OFFSET, а
    prefetch_related догружает связи только для строк страницы) или готовый
    список (режется в Python). to_data превращает строки страницы в JSON.
    """
    paginator = LimitOffsetListPagination()
    # ПОЧЕМУ cast: стабы DRF объявляют только QuerySet, но LimitOffsetPagination
    # работает с любой последовательностью — len() и срез вместо COUNT/LIMIT
    page = paginator.paginate_queryset(cast("QuerySet[Any]", source), request)
    if page is None:
        return Response(to_data(list(source)))
    return paginator.get_paginated_response(to_data(page))
