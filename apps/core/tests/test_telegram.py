from __future__ import annotations

import json
import logging
from collections.abc import Callable

import httpx
import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.core import telegram
from apps.core.telegram import TelegramDeliveryError, send_manager_message

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
