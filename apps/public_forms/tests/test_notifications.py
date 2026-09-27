from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from unittest.mock import AsyncMock

import httpx
import pytest
from asgiref.sync import async_to_sync
from django.test import override_settings

from apps.public_forms import tasks
from apps.public_forms.tasks import (
    RETRY_AFTER_CAP_SECONDS,
    NotificationDeliveryError,
    build_notification_text,
    notify_managers_task,
)
from apps.public_forms.tests.factories import (
    CallbackRequestFactory,
    FeedbackRequestFactory,
)

TelegramHandler = Callable[[httpx.Request], httpx.Response]


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {}})


@pytest.fixture(autouse=True)
def _configured() -> Iterator[None]:
    with override_settings(
        TELEGRAM_BOT_TOKEN="123:abc",
        TELEGRAM_MANAGER_CHAT_ID="-100500",
        ADMIN_BASE_URL="https://api.example.ru/",
    ):
        yield


@pytest.fixture
def telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[TelegramHandler], list[httpx.Request]]:
    def _install(handler: TelegramHandler) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def _record(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        monkeypatch.setattr(
            "apps.public_forms.services.get_http_client",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(_record)),
        )
        return seen

    return _install


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    sleep = AsyncMock()
    monkeypatch.setattr(tasks.asyncio, "sleep", sleep)
    return sleep


def _sent_text(request: httpx.Request) -> str:
    return str(json.loads(request.content)["text"])


class TestNotificationText:
    def test_feedback_text_is_number_and_admin_link(self) -> None:
        assert build_notification_text(42, "feedback") == (
            "Новое обращение с сайта #42\n"
            "https://api.example.ru/admin/public_forms/feedbackrequest/42/change/"
        )

    def test_callback_text_is_number_and_admin_link(self) -> None:
        assert build_notification_text(7, "callback") == (
            "Новая заявка на обратный звонок #7\n"
            "https://api.example.ru/admin/public_forms/callbackrequest/7/change/"
        )


# ПОЧЕМУ: синхронные тесты + async_to_sync — ORM задачи работает в главном
# потоке внутри тестовой транзакции; async-тест с transaction=True оставлял
# соединение в потоке sync_to_async и мешал удалить тестовую базу
@pytest.mark.django_db
class TestNotifyManagersTask:
    def test_feedback_sent_without_personal_data(
        self, telegram: Callable[[TelegramHandler], list[httpx.Request]]
    ) -> None:
        # ПОЧЕМУ: серверы Telegram за рубежом — имя, email и текст обращения
        # туда не уходят, менеджер открывает заявку по ссылке
        feedback = FeedbackRequestFactory(
            name="Ольга", email="olga@example.com", message="Со скольки лет английский?"
        )
        seen = telegram(_ok)

        async_to_sync(notify_managers_task.original_func)(feedback.pk, "feedback")

        (request,) = seen
        text = _sent_text(request)
        assert text == build_notification_text(feedback.pk, "feedback")
        for personal in ("Ольга", "olga@example.com", "английский"):
            assert personal not in text
        assert json.loads(request.content)["chat_id"] == "-100500"
        assert "/bot123:abc/sendMessage" in str(request.url)

    def test_callback_sent_without_phone(
        self, telegram: Callable[[TelegramHandler], list[httpx.Request]]
    ) -> None:
        callback = CallbackRequestFactory(name="Ольга", phone="+79991234567")
        seen = telegram(_ok)

        async_to_sync(notify_managers_task.original_func)(callback.pk, "callback")

        text = _sent_text(seen[0])
        assert text == build_notification_text(callback.pk, "callback")
        assert "9991234567" not in text

    def test_missing_request_is_skipped(
        self, telegram: Callable[[TelegramHandler], list[httpx.Request]]
    ) -> None:
        seen = telegram(_ok)

        async_to_sync(notify_managers_task.original_func)(999_999, "feedback")

        assert seen == []


@pytest.mark.asyncio
class TestDelivery:
    async def test_unconfigured_telegram_makes_no_calls(
        self,
        telegram: Callable[[TelegramHandler], list[httpx.Request]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        seen = telegram(_ok)

        with override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_MANAGER_CHAT_ID=""):
            await tasks._deliver("текст", "feedback", 1)

        assert seen == []
        assert "Telegram не настроен" in caplog.text

    @pytest.mark.parametrize("code", [400, 401, 403, 404])
    async def test_permanent_error_logged_not_retried(
        self,
        telegram: Callable[[TelegramHandler], list[httpx.Request]],
        caplog: pytest.LogCaptureFixture,
        code: int,
    ) -> None:
        # ПОЧЕМУ: неверный токен/чат или бот удалён из чата повтором не лечатся —
        # исключение отправило бы задачу в 5 бессмысленных ретраев
        telegram(
            lambda r: httpx.Response(
                code, json={"ok": False, "error_code": code, "description": "Forbidden"}
            )
        )

        with caplog.at_level(logging.ERROR, logger="apps.public_forms.tasks"):
            await tasks._deliver("текст", "feedback", 1)

        assert f"отклонил запрос {code} «Forbidden»" in caplog.text

    async def test_server_error_goes_to_retry(
        self, telegram: Callable[[TelegramHandler], list[httpx.Request]]
    ) -> None:
        telegram(lambda r: httpx.Response(502))

        with pytest.raises(NotificationDeliveryError):
            await tasks._deliver("текст", "feedback", 1)

    async def test_network_error_goes_to_retry(
        self, telegram: Callable[[TelegramHandler], list[httpx.Request]]
    ) -> None:
        def _down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        telegram(_down)

        with pytest.raises(NotificationDeliveryError):
            await tasks._deliver("текст", "feedback", 1)


@pytest.mark.asyncio
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
        telegram: Callable[[TelegramHandler], list[httpx.Request]],
        no_sleep: AsyncMock,
        flood_reply: httpx.Response,
        expected_pause: float,
    ) -> None:
        replies = iter([flood_reply, httpx.Response(200, json={"ok": True})])
        seen = telegram(lambda r: next(replies))

        await tasks._deliver("текст", "feedback", 1)

        no_sleep.assert_awaited_once_with(expected_pause)
        assert len(seen) == 2

    async def test_second_flood_reply_goes_to_retry(
        self,
        telegram: Callable[[TelegramHandler], list[httpx.Request]],
        no_sleep: AsyncMock,
    ) -> None:
        telegram(lambda r: httpx.Response(429, json={"parameters": {"retry_after": 1}}))

        with pytest.raises(NotificationDeliveryError):
            await tasks._deliver("текст", "feedback", 1)
