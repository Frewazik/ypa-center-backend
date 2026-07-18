from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus

from django.core.cache import cache
from rest_framework.request import Request
from rest_framework.response import Response


def _build_key(prefix: str, path: str) -> str:
    return f"payload_cache:{prefix}:{path}"


def payload_cache_key(prefix: str, request: Request) -> str:
    # ПОЧЕМУ: строго request.path, без query string — витрина не читает
    # параметры, а ?rnd=N пробивал бы кэш в БД и раздувал Redis мусорными
    # ключами. Эндпоинт с фильтрацией обязан собирать ключ сам
    return _build_key(prefix, request.path)


def invalidate_payload_cache(prefix: str, path: str) -> None:
    cache.delete(_build_key(prefix, path))


def _to_native(data: object) -> object:
    # ПОЧЕМУ: в кэш уходят чистые list/dict, а не DRF-обёртки
    # (ReturnDict/ReturnList несут backlink на serializer) — не полагаемся
    # на их __reduce__ при pickle в Redis
    if isinstance(data, dict):
        return {key: _to_native(value) for key, value in data.items()}
    if isinstance(data, list):
        return [_to_native(item) for item in data]
    return data


def cached_payload(
    *, key: str, ttl_seconds: int, produce: Callable[[], Response]
) -> Response:
    # ПОЧЕМУ: cache_page кэширует HTTP-ответ без Vary по Accept — HTML
    # browsable API отравил бы кэш для JSON-клиентов. Кэшируются
    # сериализованные данные, рендер и заголовки собираются на каждый запрос
    cached: object | None = cache.get(key)
    if cached is not None:
        return Response(cached)
    response = produce()
    if response.status_code == HTTPStatus.OK:
        cache.set(key, _to_native(response.data), ttl_seconds)
    return response
