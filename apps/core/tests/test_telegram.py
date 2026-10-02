from __future__ import annotations

import json
import logging
from collections.abc import Callable

import httpx
import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.core import telegram
from apps.core.telegram import (
    RETRY_AFTER_CAP_SECONDS,
    TelegramDeliveryError,
    send_manager_message,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def configured(settings: SettingsWrapper) -> None:
    settings.TELEGRAM_BOT_TOKEN = "bot-token"
    settings.TELEGRAM_MANAGER_CHAT_ID = "-100500"


def _route(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    # ПОЧЕМУ MockTransport: подменяется провод до api.telegram.org, а не наш код
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        telegram,
        "_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(recording)),
    )
    return seen


@pytest.mark.usefixtures("configured")
class TestSendManagerMessage:
    async def test_posts_text_to_manager_chat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _route(monkeypatch, lambda r: httpx.Response(200, json={"ok": True}))

        await send_manager_message("Привет", context="test")

        assert seen[0].url.path == "/botbot-token/sendMessage"
        assert json.loads(seen[0].content) == {"chat_id": "-100500", "text": "Привет"}

    async def test_rate_limit_waits_and_retries_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answers = iter(
            [httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200)]
        )
        seen = _route(monkeypatch, lambda r: next(answers))
        waited: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            waited.append(seconds)

        monkeypatch.setattr(telegram.asyncio, "sleep", fake_sleep)

        await send_manager_message("Привет", context="test")

        assert len(seen) == 2
        assert waited == [3.0]

    async def test_server_error_is_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _route(monkeypatch, lambda r: httpx.Response(502))

        with pytest.raises(TelegramDeliveryError):
            await send_manager_message("Привет", context="test")

    async def test_network_error_is_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("нет сети")

        _route(monkeypatch, broken)

        with pytest.raises(TelegramDeliveryError):
            await send_manager_message("Привет", context="test")

    @pytest.mark.parametrize("code", [400, 401, 403, 404])
    async def test_permanent_error_logged_not_retried(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        code: int,
    ) -> None:
        # ПОЧЕМУ: неверный токен/чат или бот удалён из чата повтором не лечатся —
        # исключение отправило бы задачу в 5 бессмысленных ретраев
        _route(
            monkeypatch,
            lambda r: httpx.Response(
                code, json={"ok": False, "error_code": code, "description": "Forbidden"}
            ),
        )

        with caplog.at_level(logging.ERROR, logger="apps.core.telegram"):
            await send_manager_message("Привет", context="form=feedback id=1")

        assert f"отклонил запрос {code} «Forbidden»" in caplog.text


@pytest.mark.usefixtures("configured")
class TestFloodLimit:
    @pytest.mark.parametrize(
        ("flood_reply", "expected_pause"),
        [
            # Так отвечает Telegram: пауза в теле ответа
            (
                httpx.Response(
                    429, json={"ok": False, "parameters": {"retry_after": 7}}
                ),
                7.0,
            ),
            # Запасной вариант — заголовок
            (httpx.Response(429, headers={"Retry-After": "3"}), 3.0),
            # Мусор в заголовке не роняет задачу
            (
                httpx.Response(
                    429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
                ),
                1.0,
            ),
            (httpx.Response(429), 1.0),
            # Долгое ожидание держало бы слот воркера — дальше решает ретрай брокера
            (
                httpx.Response(429, json={"parameters": {"retry_after": 600}}),
                RETRY_AFTER_CAP_SECONDS,
            ),
        ],
    )
    async def test_waits_as_telegram_asks_then_resends(
        self,
        monkeypatch: pytest.MonkeyPatch,
        flood_reply: httpx.Response,
        expected_pause: float,
    ) -> None:
        replies = iter([flood_reply, httpx.Response(200, json={"ok": True})])
        seen = _route(monkeypatch, lambda r: next(replies))
        waited: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            waited.append(seconds)

        monkeypatch.setattr(telegram.asyncio, "sleep", fake_sleep)

        await send_manager_message("Привет", context="test")

        assert waited == [expected_pause]
        assert len(seen) == 2

    async def test_second_flood_reply_goes_to_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _route(
            monkeypatch,
            lambda r: httpx.Response(429, json={"parameters": {"retry_after": 1}}),
        )

        async def fake_sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr(telegram.asyncio, "sleep", fake_sleep)

        with pytest.raises(TelegramDeliveryError):
            await send_manager_message("Привет", context="test")


async def test_unconfigured_channel_logs_and_skips(
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings.TELEGRAM_BOT_TOKEN = ""
    seen = _route(monkeypatch, lambda r: httpx.Response(200))

    with caplog.at_level(logging.ERROR, logger="apps.core.telegram"):
        await send_manager_message("Привет", context="form=callback id=7")

    assert seen == []
    assert "не настроен" in caplog.text
    assert "form=callback id=7" in caplog.text
