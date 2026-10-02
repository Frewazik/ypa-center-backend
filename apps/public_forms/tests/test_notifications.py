from __future__ import annotations

import json
from collections.abc import Callable, Iterator

import httpx
import pytest
from asgiref.sync import async_to_sync
from django.test import override_settings

from apps.core import telegram as telegram_channel
from apps.public_forms.tasks import build_notification_text, notify_managers_task
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

        # Доставка и её ошибки — общий канал, тесты в apps/core/tests/test_telegram.py
        monkeypatch.setattr(
            telegram_channel,
            "_http_client",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(_record)),
        )
        return seen

    return _install


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
