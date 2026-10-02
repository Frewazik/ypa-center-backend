# Контракт HTTP-адаптера ЮКассы. Подменяется только провод — транспорт httpx
# (httpx.MockTransport); собственный код адаптера и сервисов работает как в проде.

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import timedelta

import httpx
import pytest
from django.utils import timezone

from apps.billing import adapters
from apps.billing.adapters import (
    GatewayContractError,
    GatewayNetworkError,
    InvalidPaymentIdError,
    PaymentNotFoundError,
    YookassaHttpGateway,
    YookassaSettings,
)
from apps.billing.models import Subscription, SubscriptionStatus, Transaction
from apps.billing.services import sweep_stale_pending_transactions
from apps.billing.tests.test_billing import (
    FakeSchedulePort,
    _checkout,
    _make_pending_payment,
)
from apps.billing.tests.test_trials import _trial_checkout

_SETTINGS = YookassaSettings(shop_id="test-shop", secret_key="test-secret")
_PAYMENT_ID = "2e8f3c1a-000f-5000-9000-1db2a1a1e0c1"

Handler = Callable[[httpx.Request], httpx.Response]


def _gateway(handler: Handler) -> YookassaHttpGateway:
    return YookassaHttpGateway(
        settings=_SETTINGS, transport=httpx.MockTransport(handler)
    )


def _payment_json(
    status: str, *, transaction_id: str = "tid", value: str = "7000.00"
) -> dict[str, object]:
    return {
        "id": _PAYMENT_ID,
        "status": status,
        "amount": {"value": value, "currency": "RUB"},
        "metadata": {"transaction_id": transaction_id},
    }


class TestGetPayment:
    def test_sends_authorized_get_and_parses_payment(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=_payment_json("succeeded"))

        info = _gateway(handler).get_payment(_PAYMENT_ID)

        assert info.status == "succeeded"
        assert info.amount_kopecks == 700_000
        assert info.transaction_id == "tid"
        (request,) = seen
        assert request.method == "GET"
        assert request.url.path == f"/v3/payments/{_PAYMENT_ID}"
        expected = base64.b64encode(b"test-shop:test-secret").decode()
        assert request.headers["Authorization"] == f"Basic {expected}"

    @pytest.mark.parametrize(
        ("status_code", "error"),
        [
            (404, PaymentNotFoundError),
            (401, GatewayContractError),
            (500, GatewayNetworkError),
            (503, GatewayNetworkError),
        ],
    )
    def test_classifies_http_errors(
        self, status_code: int, error: type[Exception]
    ) -> None:
        gateway = _gateway(lambda _: httpx.Response(status_code))

        with pytest.raises(error):
            gateway.get_payment(_PAYMENT_ID)

    def test_connection_failure_is_retryable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("таймаут", request=request)

        with pytest.raises(GatewayNetworkError):
            _gateway(handler).get_payment(_PAYMENT_ID)

    def test_rejects_path_traversal_without_network(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("запрос не должен уйти в сеть")

        with pytest.raises(InvalidPaymentIdError):
            _gateway(handler).get_payment("../refunds")

    def test_broken_body_is_contract_error(self) -> None:
        gateway = _gateway(lambda _: httpx.Response(200, content=b"not json"))

        with pytest.raises(GatewayContractError):
            gateway.get_payment(_PAYMENT_ID)


class TestCreateRefund:
    def test_posts_refund_with_idempotence_key(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"id": "rf-1", "status": "succeeded"})

        refund = _gateway(handler).create_refund(_PAYMENT_ID, 700_000, "refund-tx")

        assert refund.id == "rf-1"
        assert refund.status == "succeeded"
        (request,) = seen
        assert request.method == "POST"
        assert request.url.path == "/v3/refunds"
        assert request.headers["Idempotence-Key"] == "refund-tx"
        assert json.loads(request.content) == {
            "payment_id": _PAYMENT_ID,
            "amount": {"value": "7000.00", "currency": "RUB"},
        }

    @pytest.mark.parametrize(
        ("status_code", "error"),
        [
            (404, PaymentNotFoundError),
            (400, GatewayContractError),
            (502, GatewayNetworkError),
        ],
    )
    def test_classifies_http_errors(
        self, status_code: int, error: type[Exception]
    ) -> None:
        gateway = _gateway(lambda _: httpx.Response(status_code))

        with pytest.raises(error):
            gateway.create_refund(_PAYMENT_ID, 100, "refund-tx")


class TestConnectionPooling:
    def test_gateways_without_transport_share_one_client(self) -> None:
        # ПОЧЕМУ: пул соединений живёт на процесс — новый шлюз на каждый вызов
        # задачи не открывает новое TCP/TLS-соединение
        assert adapters._shared_http_client() is adapters._shared_http_client()


@pytest.mark.django_db
class TestSweeperReconciliationOverHttp:
    def test_paid_order_without_webhook_is_activated_through_real_adapter(
        self,
    ) -> None:
        tx = _make_pending_payment([101])
        Transaction.objects.filter(pk=tx.pk).update(
            created_at=timezone.now() - timedelta(minutes=20)
        )
        tx.refresh_from_db()
        assert tx.external_id is not None

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == f"/v3/payments/{tx.external_id}"
            payment = _payment_json(
                "succeeded",
                transaction_id=str(tx.pk),
                value=adapters._kopecks_to_value(tx.amount),
            )
            payment["id"] = tx.external_id
            return httpx.Response(200, json=payment)

        sweep_stale_pending_transactions(
            gateway=_gateway(handler), schedule_port=FakeSchedulePort()
        )

        assert (
            Subscription.objects.get(pk=tx.subscription_id).status
            == SubscriptionStatus.ACTIVE
        )


def _capture_created_payment(sent: list[dict[str, object]]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(
            200,
            json={
                "id": _PAYMENT_ID,
                "status": "pending",
                "confirmation": {"confirmation_url": "https://yoomoney.ru/pay"},
            },
        )

    return handler


@pytest.mark.django_db
class TestReturnUrlCarriesTransaction:
    # ПОЧЕМУ: страница «результат оплаты» узнаёт заказ только из адреса возврата

    def test_subscription_checkout_sends_tx_in_return_url(self) -> None:
        sent: list[dict[str, object]] = []

        result = _checkout(
            [101],
            gateway=_gateway(_capture_created_payment(sent)),  # type: ignore[arg-type]
        )

        assert sent[0]["confirmation"] == {
            "type": "redirect",
            "return_url": f"{_SETTINGS.return_url}?tx={result.transaction_id}",
        }

    def test_trial_checkout_sends_tx_in_return_url(self) -> None:
        sent: list[dict[str, object]] = []

        result, _, _ = _trial_checkout(
            101,
            gateway=_gateway(_capture_created_payment(sent)),  # type: ignore[arg-type]
        )

        confirmation = sent[0]["confirmation"]
        assert isinstance(confirmation, dict)
        assert confirmation["return_url"].endswith(f"?tx={result.transaction_id}")

    def test_existing_query_and_fragment_survive(self) -> None:
        url = adapters._return_url_for(
            "https://site.ru/checkout/result?utm=ya&tx=old#top", "abc"
        )

        assert url == "https://site.ru/checkout/result?utm=ya&tx=abc#top"
