from __future__ import annotations

from typing import Any

import pytest
from django.conf import settings
from drf_spectacular.generators import SchemaGenerator

_HTTP_METHODS = frozenset(("get", "post", "put", "patch", "delete"))


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return SchemaGenerator().get_schema(request=None, public=True)


def test_every_operation_has_known_tag(schema: dict[str, Any]) -> None:
    # ПОЧЕМУ: без явного тега drf-spectacular молча кладёт ручку в общую
    # группу «v1» — фронт снова ищет её в куче. --fail-on-warn этого не ловит
    known = {tag["name"] for tag in settings.SPECTACULAR_SETTINGS["TAGS"]}
    untagged = [
        f"{method.upper()} {path}: {operation.get('tags')}"
        for path, item in schema["paths"].items()
        for method, operation in item.items()
        if method in _HTTP_METHODS
        and (len(operation.get("tags", [])) != 1 or operation["tags"][0] not in known)
    ]
    assert not untagged, (
        f"Нужен ровно один тег из SPECTACULAR_SETTINGS['TAGS']: {untagged}"
    )


def test_checkout_documents_idempotency_header(schema: dict[str, Any]) -> None:
    for path in ("/api/v1/checkout/subscription", "/api/v1/checkout/trial"):
        parameters = schema["paths"][path]["post"]["parameters"]
        header = next(p for p in parameters if p["in"] == "header")
        assert header["name"] == "X-Idempotency-Key"
        assert header["required"] is True


def test_plan_price_per_session_is_nullable(schema: dict[str, Any]) -> None:
    # ПОЧЕМУ: у безлимита цены занятия нет — без nullable генератор типов
    # фронта объявит поле числом и упадёт на null
    field = schema["components"]["schemas"]["SubscriptionPlanPublic"]["properties"][
        "price_per_session"
    ]
    assert field["nullable"] is True
