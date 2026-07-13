from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from unittest.mock import MagicMock

import httpx
import pytest
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient

from apps.public_forms.models import (
    CallbackRequest,
    CallbackStatus,
    CallTimeWindow,
    FeedbackRequest,
    FeedbackStatus,
)
from apps.public_forms.services import verify_captcha_token
from apps.public_forms.tests.factories import CallbackRequestFactory, FeedbackRequestFactory

pytestmark = [pytest.mark.django_db, pytest.mark.urls("apps.public_forms.urls")]

CALLBACK_URL = "/callback/"
FEEDBACK_URL = "/feedback/"
THROTTLE_LIMIT = 3

OnCommitCapture = Callable[..., AbstractContextManager[list[object]]]


@pytest.fixture(autouse=True)
def _isolated_throttle_cache() -> Iterator[None]:
    # ПОЧЕМУ: свой LocMemCache на каждый тест, потому что cache.clear()
    # ломал бы счётчики соседних xdist-воркеров
    unique_location = f"throttle-{uuid.uuid4()}"
    with override_settings(
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": unique_location,
            }
        }
    ):
        yield


@pytest.fixture(autouse=True)
def _captcha_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(token: str, remote_ip: str | None) -> bool:
        return True

    monkeypatch.setattr("apps.public_forms.services.verify_captcha_token", _ok)


@pytest.fixture(autouse=True)
def notify_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()

    async def fake_kiq(*args: object, **kwargs: object) -> None:
        mock(*args, **kwargs)

    monkeypatch.setattr("apps.public_forms.services.notify_managers_task.kiq", fake_kiq)
    return mock


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def callback_payload() -> dict[str, str]:
    return {
        "name": "Ольга",
        "phone": "+79991234567",
        "preferred_time_window": CallTimeWindow.MORNING.value,
        "captcha_token": "test-token",
    }


@pytest.fixture
def feedback_payload() -> dict[str, str]:
    return {
        "name": "Ольга",
        "email": "olga@example.com",
        "message": "Со скольки лет принимаете на английский язык?",
        "captcha_token": "test-token",
    }


class TestHoneypot:
    def test_callback_honeypot_returns_success_but_saves_nothing(
        self,
        api_client: APIClient,
        callback_payload: dict[str, str],
        notify_mock: MagicMock,
    ) -> None:
        payload = {**callback_payload, "website_url": "https://spam.example"}

        response = api_client.post(CALLBACK_URL, payload, format="json")

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert response.json() == {"status": "accepted"}
        assert CallbackRequest.objects.count() == 0
        notify_mock.assert_not_called()

    def test_feedback_honeypot_returns_success_but_saves_nothing(
        self,
        api_client: APIClient,
        feedback_payload: dict[str, str],
        notify_mock: MagicMock,
    ) -> None:
        payload = {**feedback_payload, "website_url": "https://spam.example"}

        response = api_client.post(FEEDBACK_URL, payload, format="json")

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert response.json() == {"status": "accepted"}
        assert FeedbackRequest.objects.count() == 0
        notify_mock.assert_not_called()

    def test_honeypot_skips_captcha_entirely(
        self,
        api_client: APIClient,
        callback_payload: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captcha_called = MagicMock()

        async def _fail(token: str, remote_ip: str | None) -> bool:
            captcha_called()
            return False

        monkeypatch.setattr("apps.public_forms.services.verify_captcha_token", _fail)
        payload = {**callback_payload, "website_url": "https://spam.example"}

        response = api_client.post(CALLBACK_URL, payload, format="json")

        assert response.status_code == status.HTTP_202_ACCEPTED
        captcha_called.assert_not_called()


class TestThrottling:
    def test_callback_ip_throttle_blocks_after_limit(
        self, api_client: APIClient, callback_payload: dict[str, str]
    ) -> None:
        for _ in range(THROTTLE_LIMIT):
            ok = api_client.post(CALLBACK_URL, callback_payload, format="json")
            assert ok.status_code == status.HTTP_202_ACCEPTED

        blocked = api_client.post(CALLBACK_URL, callback_payload, format="json")

        assert blocked.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert CallbackRequest.objects.count() == THROTTLE_LIMIT

    def test_throttle_scopes_are_independent_between_forms(
        self,
        api_client: APIClient,
        callback_payload: dict[str, str],
        feedback_payload: dict[str, str],
    ) -> None:
        for _ in range(THROTTLE_LIMIT):
            api_client.post(CALLBACK_URL, callback_payload, format="json")

        response = api_client.post(FEEDBACK_URL, feedback_payload, format="json")

        assert response.status_code == status.HTTP_202_ACCEPTED

    def test_throttle_keys_by_forwarded_client_ip_not_proxy(
        self, api_client: APIClient, callback_payload: dict[str, str]
    ) -> None:
        # ПОЧЕМУ: разные клиентские IP в X-Forwarded-For не должны делить
        # один счётчик, даже если REMOTE_ADDR (адрес прокси) одинаков
        for _ in range(THROTTLE_LIMIT):
            api_client.post(
                CALLBACK_URL, callback_payload, format="json",
                HTTP_X_FORWARDED_FOR="203.0.113.1",
            )
        blocked = api_client.post(
            CALLBACK_URL, callback_payload, format="json",
            HTTP_X_FORWARDED_FOR="203.0.113.1",
        )
        other_client = api_client.post(
            CALLBACK_URL, callback_payload, format="json",
            HTTP_X_FORWARDED_FOR="203.0.113.2",
        )

        assert blocked.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert other_client.status_code == status.HTTP_202_ACCEPTED


@pytest.mark.asyncio
class TestCaptchaTokenVerification:
    @staticmethod
    def _patch_client(
        monkeypatch: pytest.MonkeyPatch,
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr("apps.public_forms.services.get_http_client", lambda: client)

    async def test_non_json_200_response_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ПОЧЕМУ: WAF-заглушка с 200 и HTML не должна ронять систему в 500
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>Attention Required</html>")

        self._patch_client(monkeypatch, _handler)

        assert await verify_captcha_token("some-token", "203.0.113.1") is False

    async def test_empty_token_short_circuits_without_network(self) -> None:
        assert await verify_captcha_token("", "203.0.113.1") is False

    async def test_network_error_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        self._patch_client(monkeypatch, _handler)

        assert await verify_captcha_token("some-token", "203.0.113.1") is False

    async def test_valid_success_response_returns_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"success": True})

        self._patch_client(monkeypatch, _handler)

        assert await verify_captcha_token("good-token", "203.0.113.1") is True


class TestCaptcha:
    def test_invalid_captcha_rejects_request(
        self,
        api_client: APIClient,
        callback_payload: dict[str, str],
        notify_mock: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _fail(token: str, remote_ip: str | None) -> bool:
            return False

        monkeypatch.setattr("apps.public_forms.services.verify_captcha_token", _fail)

        response = api_client.post(CALLBACK_URL, callback_payload, format="json")

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert CallbackRequest.objects.count() == 0
        notify_mock.assert_not_called()


class TestHappyPath:
    def test_callback_created_and_notification_scheduled_on_commit(
        self,
        api_client: APIClient,
        callback_payload: dict[str, str],
        notify_mock: MagicMock,
        django_capture_on_commit_callbacks: OnCommitCapture,
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            response = api_client.post(CALLBACK_URL, callback_payload, format="json")

        assert response.status_code == status.HTTP_202_ACCEPTED
        instance = CallbackRequest.objects.get()
        assert str(instance.phone) == "+79991234567"
        assert instance.status == CallbackStatus.NEW
        notify_mock.assert_called_once_with(instance.pk, "callback")

    def test_feedback_created_and_notification_scheduled_on_commit(
        self,
        api_client: APIClient,
        feedback_payload: dict[str, str],
        notify_mock: MagicMock,
        django_capture_on_commit_callbacks: OnCommitCapture,
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            response = api_client.post(FEEDBACK_URL, feedback_payload, format="json")

        assert response.status_code == status.HTTP_202_ACCEPTED
        instance = FeedbackRequest.objects.get()
        assert instance.email == "olga@example.com"
        assert instance.status == FeedbackStatus.NEW
        notify_mock.assert_called_once_with(instance.pk, "feedback")

    def test_notification_not_scheduled_before_commit(
        self,
        api_client: APIClient,
        callback_payload: dict[str, str],
        notify_mock: MagicMock,
        django_capture_on_commit_callbacks: OnCommitCapture,
    ) -> None:
        with django_capture_on_commit_callbacks(execute=False):
            api_client.post(CALLBACK_URL, callback_payload, format="json")

        notify_mock.assert_not_called()


class TestModels:
    def test_history_records_created_for_audit(self) -> None:
        callback = CallbackRequestFactory()
        feedback = FeedbackRequestFactory()

        assert callback.history.count() == 1
        assert feedback.history.count() == 1

    def test_status_transition_is_audited(self) -> None:
        callback = CallbackRequestFactory()

        callback.status = CallbackStatus.IN_PROGRESS
        callback.save()

        assert callback.history.count() == 2
        assert callback.history.earliest().status == CallbackStatus.NEW