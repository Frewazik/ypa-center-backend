from __future__ import annotations

import pytest
from django.conf import settings
from django.test import Client

_CHECKOUT_PATH = "/api/v1/checkout/subscription"
_FOREIGN_ORIGIN = "https://evil.example.com"


def _allowed_origin() -> str:
    return settings.CORS_ALLOWED_ORIGINS[0]


def _preflight(origin: str):  # noqa: ANN202 — HttpResponse, тесты вне mypy-скоупа
    return Client().options(
        _CHECKOUT_PATH,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,"
            "x-idempotency-key",
        },
    )


@pytest.mark.django_db
class TestCheckoutCors:
    def test_preflight_allows_idempotency_header_for_known_origin(self) -> None:
        response = _preflight(_allowed_origin())

        assert response.status_code == 200
        assert response["Access-Control-Allow-Origin"] == _allowed_origin()
        allowed = response["Access-Control-Allow-Headers"].lower()
        assert "x-idempotency-key" in allowed
        # Стандартный набор не потерян: заменили бы его — отвалился бы JWT
        assert "authorization" in allowed
        assert "content-type" in allowed

    def test_preflight_rejects_foreign_origin(self) -> None:
        response = _preflight(_FOREIGN_ORIGIN)

        # ПОЧЕМУ так: django-cors-headers не отвечает 403 — он просто не
        # ставит Allow-Origin, и браузер сам блокирует запрос
        assert "Access-Control-Allow-Origin" not in response

    def test_retry_after_and_request_id_exposed(self) -> None:
        response = Client().get(
            "/api/v1/me/profile", headers={"Origin": _allowed_origin()}
        )

        exposed = [
            h.strip().lower()
            for h in response["Access-Control-Expose-Headers"].split(",")
        ]
        assert "retry-after" in exposed
        assert "x-request-id" in exposed
