from __future__ import annotations

from collections.abc import Iterator
from typing import cast
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest
from django.test import override_settings
from rest_framework.request import Request
from rest_framework.test import APIClient, APIRequestFactory
from rest_framework.views import APIView

from apps.billing.permissions import YookassaIPAllowlist
from apps.core.net import client_ip
from config.settings import Settings, _resolve_trusted_proxy_count

PROXY_ADDR = "172.18.0.2"
YOOKASSA_IP = "185.71.76.5"


def _proxies(count: int) -> override_settings:
    return override_settings(
        REST_FRAMEWORK={**settings.REST_FRAMEWORK, "NUM_PROXIES": count}
    )


@pytest.fixture
def behind_one_proxy() -> Iterator[None]:
    with _proxies(1):
        yield


def _request(remote_addr: str, forwarded: str | None = None) -> HttpRequest:
    extra = {"REMOTE_ADDR": remote_addr}
    if forwarded is not None:
        extra["HTTP_X_FORWARDED_FOR"] = forwarded
    return APIRequestFactory().post("/", **extra)


class TestClientIp:
    def test_without_proxy_trusts_only_tcp_address(self) -> None:
        request = _request("203.0.113.7", forwarded="1.2.3.4")

        assert client_ip(request) == "203.0.113.7"

    @pytest.mark.usefixtures("behind_one_proxy")
    @pytest.mark.parametrize(
        ("forwarded", "expected"),
        [
            # Прокси затёр заголовок клиента своим значением
            ("203.0.113.7", "203.0.113.7"),
            # Прокси дописал реальный адрес справа к подделке клиента
            ("1.2.3.4, 203.0.113.7", "203.0.113.7"),
            (" 1.2.3.4 ,203.0.113.7 ", "203.0.113.7"),
        ],
    )
    def test_behind_proxy_takes_address_added_by_proxy(
        self, forwarded: str, expected: str
    ) -> None:
        assert client_ip(_request(PROXY_ADDR, forwarded=forwarded)) == expected

    @pytest.mark.usefixtures("behind_one_proxy")
    def test_behind_proxy_without_header_falls_back_to_tcp_address(self) -> None:
        assert client_ip(_request(PROXY_ADDR)) == PROXY_ADDR

    def test_two_proxies_skip_one_more_hop(self) -> None:
        # Облачный балансировщик → Caddy → Django
        request = _request(PROXY_ADDR, forwarded="1.2.3.4, 203.0.113.7, 10.0.0.5")

        with _proxies(2):
            assert client_ip(request) == "203.0.113.7"


class TestTrustedProxyCountSetting:
    @staticmethod
    def _env(**overrides: object) -> Settings:
        # _env_file=None — не подмешивать локальный .env разработчика
        return Settings(SECRET_KEY="test", _env_file=None, **overrides)  # type: ignore[call-arg]

    def test_local_defaults_to_no_proxy(self) -> None:
        assert _resolve_trusted_proxy_count(self._env(ENVIRONMENT="local")) == 0

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_required_outside_local(self, environment: str) -> None:
        # ПОЧЕМУ: забытая переменная должна ронять запуск, а не молча
        # открывать подмену IP или склеивать всех клиентов в один лимит
        with pytest.raises(ImproperlyConfigured):
            _resolve_trusted_proxy_count(self._env(ENVIRONMENT=environment))

    def test_explicit_value_wins(self) -> None:
        env = self._env(ENVIRONMENT="production", TRUSTED_PROXY_COUNT=1)

        assert _resolve_trusted_proxy_count(env) == 1


class TestYookassaAllowlistBehindProxy:
    @staticmethod
    def _allowed(remote_addr: str, forwarded: str | None = None) -> bool:
        # Permission читает только META — сырого запроса фабрики достаточно
        request = cast(Request, _request(remote_addr, forwarded))
        return YookassaIPAllowlist().has_permission(request, cast(APIView, None))

    @pytest.mark.usefixtures("behind_one_proxy")
    def test_webhook_through_proxy_is_accepted(self) -> None:
        # ПОЧЕМУ: раньше проверялся голый REMOTE_ADDR — за прокси это адрес
        # самого прокси, и все настоящие вебхуки получали бы 403
        assert self._allowed(PROXY_ADDR, forwarded=YOOKASSA_IP) is True

    @pytest.mark.usefixtures("behind_one_proxy")
    def test_forged_yookassa_ip_in_header_is_rejected(self) -> None:
        # Злоумышленник 203.0.113.10 вписал IP ЮКассы слева — прокси
        # дописал его настоящий адрес справа
        forged = f"{YOOKASSA_IP}, 203.0.113.10"

        assert self._allowed(PROXY_ADDR, forwarded=forged) is False

    def test_without_proxy_header_cannot_fake_yookassa(self) -> None:
        assert self._allowed("203.0.113.10", forwarded=YOOKASSA_IP) is False

    @pytest.mark.usefixtures("behind_one_proxy")
    def test_garbage_in_header_is_rejected(self) -> None:
        assert self._allowed(PROXY_ADDR, forwarded="unknown") is False


@pytest.mark.django_db
class TestOtpLimitBehindProxy:
    @pytest.fixture(autouse=True)
    def _clear_cache(self) -> None:
        from django.core.cache import cache

        cache.clear()

    @pytest.mark.usefixtures("behind_one_proxy")
    def test_forged_forwarded_prefix_does_not_reset_ip_limit(self) -> None:
        # ПОЧЕМУ: раньше DRF без NUM_PROXIES брал X-Forwarded-For целиком —
        # новый мусор слева на каждый запрос давал новый счётчик
        rate = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["otp_request_ip"]
        limit = int(rate.split("/")[0])
        client = APIClient()

        with patch("apps.users.views.request_otp"):
            statuses = [
                client.post(
                    "/api/v1/auth/otp/request/",
                    {"email": f"victim{i}@example.com"},
                    format="json",
                    REMOTE_ADDR=PROXY_ADDR,
                    HTTP_X_FORWARDED_FOR=f"10.9.{i}.1, 203.0.113.7",
                ).status_code
                for i in range(limit + 1)
            ]

        assert statuses[:limit] == [202] * limit
        assert statuses[-1] == 429
