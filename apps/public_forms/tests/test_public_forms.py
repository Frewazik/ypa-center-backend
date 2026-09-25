from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from unittest.mock import MagicMock

import httpx
import pytest
from django.conf import settings
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
from apps.users.models import ConsentPurpose, PersonalDataConsent
from apps.public_forms.tests.factories import (
    CallbackRequestFactory,
    FeedbackRequestFactory,
)

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
def callback_payload() -> dict[str, object]:
    return {
        "name": "Ольга",
        "phone": "+79991234567",
        "preferred_time_window": CallTimeWindow.MORNING.value,
        "pd_consent": True,
        "captcha_token": "test-token",
    }


@pytest.fixture
def feedback_payload() -> dict[str, object]:
    return {
        "name": "Ольга",
        "email": "olga@example.com",
        "message": "Со скольки лет принимаете на английский язык?",
        "pd_consent": True,
        "captcha_token": "test-token",
    }


class TestHoneypot:
    def test_callback_honeypot_returns_success_but_saves_nothing(
        self,
        api_client: APIClient,
        callback_payload: dict[str, object],
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
        feedback_payload: dict[str, object],
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
        callback_payload: dict[str, object],
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
        self, api_client: APIClient, callback_payload: dict[str, object]
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
        callback_payload: dict[str, object],
        feedback_payload: dict[str, object],
    ) -> None:
        for _ in range(THROTTLE_LIMIT):
            api_client.post(CALLBACK_URL, callback_payload, format="json")

        response = api_client.post(FEEDBACK_URL, feedback_payload, format="json")

        assert response.status_code == status.HTTP_202_ACCEPTED

    def test_throttle_keys_by_client_ip_behind_own_proxy(
        self, api_client: APIClient, callback_payload: dict[str, object]
    ) -> None:
        # ПОЧЕМУ: за своим прокси REMOTE_ADDR у всех один (адрес прокси) —
        # клиентов различает адрес, который прокси дописал в X-Forwarded-For
        def post(forwarded: str) -> int:
            return api_client.post(
                CALLBACK_URL,
                callback_payload,
                format="json",
                REMOTE_ADDR="172.18.0.2",
                HTTP_X_FORWARDED_FOR=forwarded,
            ).status_code

        with override_settings(
            REST_FRAMEWORK={**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}
        ):
            for _ in range(THROTTLE_LIMIT):
                post("203.0.113.1")
            blocked = post("203.0.113.1")
            # Подделка: клиент 203.0.113.1 вписал слева «чужой» IP —
            # прокси дописал настоящий справа, лимит не обойти
            spoofed = post("198.51.100.77, 203.0.113.1")
            other_client = post("203.0.113.2")

        assert blocked == status.HTTP_429_TOO_MANY_REQUESTS
        assert spoofed == status.HTTP_429_TOO_MANY_REQUESTS
        assert other_client == status.HTTP_202_ACCEPTED

    def test_forwarded_header_ignored_without_own_proxy(
        self, api_client: APIClient, callback_payload: dict[str, object]
    ) -> None:
        # ПОЧЕМУ: без своего прокси X-Forwarded-For целиком пишет клиент —
        # новый адрес в заголовке на каждый запрос не сбрасывает лимит
        statuses = [
            api_client.post(
                CALLBACK_URL,
                callback_payload,
                format="json",
                HTTP_X_FORWARDED_FOR=f"203.0.113.{i + 1}",
            ).status_code
            for i in range(THROTTLE_LIMIT + 1)
        ]

        assert statuses[-1] == status.HTTP_429_TOO_MANY_REQUESTS


@pytest.mark.asyncio
class TestCaptchaTokenVerification:
    @staticmethod
    def _patch_client(
        monkeypatch: pytest.MonkeyPatch,
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(
            "apps.public_forms.services.get_http_client", lambda: client
        )

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
        callback_payload: dict[str, object],
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
        callback_payload: dict[str, object],
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
        feedback_payload: dict[str, object],
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
        callback_payload: dict[str, object],
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


class TestPersonalDataConsent:
    @pytest.mark.parametrize("consent", [None, False])
    @pytest.mark.parametrize(
        ("url", "payload_fixture", "model"),
        [
            (CALLBACK_URL, "callback_payload", CallbackRequest),
            (FEEDBACK_URL, "feedback_payload", FeedbackRequest),
        ],
    )
    def test_form_without_consent_is_rejected_and_not_saved(
        self,
        api_client: APIClient,
        request: pytest.FixtureRequest,
        url: str,
        payload_fixture: str,
        model: type[CallbackRequest] | type[FeedbackRequest],
        consent: bool | None,
    ) -> None:
        payload = dict(request.getfixturevalue(payload_fixture))
        if consent is None:
            payload.pop("pd_consent")
        else:
            payload["pd_consent"] = consent

        response = api_client.post(url, payload, format="json")

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert model.objects.count() == 0
        assert PersonalDataConsent.objects.count() == 0

    def test_callback_consent_journaled_with_link_to_request(
        self, api_client: APIClient, callback_payload: dict[str, object]
    ) -> None:
        response = api_client.post(
            CALLBACK_URL,
            callback_payload,
            format="json",
            REMOTE_ADDR="203.0.113.9",
            HTTP_USER_AGENT="Mozilla/5.0",
        )

        assert response.status_code == status.HTTP_202_ACCEPTED
        record = PersonalDataConsent.objects.get()
        assert record.purpose == ConsentPurpose.CALLBACK
        assert record.source_id == CallbackRequest.objects.get().pk
        assert str(record.phone) == callback_payload["phone"]
        assert record.document_version == settings.PD_CONSENT_VERSION
        assert record.ip == "203.0.113.9"
        assert record.parent is None

    def test_feedback_consent_journaled_with_email(
        self, api_client: APIClient, feedback_payload: dict[str, object]
    ) -> None:
        api_client.post(FEEDBACK_URL, feedback_payload, format="json")

        record = PersonalDataConsent.objects.get()
        assert record.purpose == ConsentPurpose.FEEDBACK
        assert record.source_id == FeedbackRequest.objects.get().pk
        assert record.email == feedback_payload["email"]

    def test_honeypot_drop_leaves_no_consent_record(
        self, api_client: APIClient, callback_payload: dict[str, object]
    ) -> None:
        # ПОЧЕМУ: данные бота не сохраняются — и согласие фиксировать не на что
        payload = {**callback_payload, "website_url": "https://spam.example"}

        api_client.post(CALLBACK_URL, payload, format="json")

        assert PersonalDataConsent.objects.count() == 0
