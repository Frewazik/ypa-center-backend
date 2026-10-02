from __future__ import annotations

from importlib import import_module
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from django.apps import apps as django_apps
from django.core import mail
from pytest_django.fixtures import DjangoCaptureOnCommitCallbacks

from apps.billing.adapters import (
    GatewayContractError,
    GatewayNetworkError,
    YookassaHttpGateway,
    YookassaSettings,
)
from apps.billing.models import RefundStatus, Transaction
from apps.billing.services import issue_pending_refunds, sync_pending_refunds
from apps.billing.tasks import send_refund_email_task
from apps.billing.tests.test_billing import FakeGateway, _make_refundable_payment

_SETTINGS = YookassaSettings(shop_id="shop", secret_key="secret")


def _issued_pending(gateway: FakeGateway) -> Transaction:
    # Возврат принят ЮКассой, но ещё не завершён
    tx = _make_refundable_payment([101])
    gateway.new_refund_status = "pending"
    assert issue_pending_refunds(gateway=gateway) == 1
    tx.refresh_from_db()
    assert tx.refund_status == RefundStatus.PENDING
    return tx


@pytest.mark.django_db
class TestSyncPendingRefunds:
    def test_succeeded_refund_is_settled_and_parent_notified(
        self, django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks
    ) -> None:
        gateway = FakeGateway()
        tx = _issued_pending(gateway)
        gateway.refund_outcomes["rf-1"] = "succeeded"

        with patch("apps.billing.tasks.send_refund_email_task") as task:
            task.kiq = AsyncMock()
            with django_capture_on_commit_callbacks(execute=True):
                assert sync_pending_refunds(gateway=gateway) == 1

        tx.refresh_from_db()
        assert tx.refund_status == RefundStatus.SUCCEEDED
        task.kiq.assert_awaited_once_with(str(tx.pk))

    def test_canceled_refund_goes_to_manual_review_without_email(
        self, django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks
    ) -> None:
        gateway = FakeGateway()
        tx = _issued_pending(gateway)
        gateway.refund_outcomes["rf-1"] = "canceled"

        with patch("apps.billing.tasks.send_refund_email_task") as task:
            task.kiq = AsyncMock()
            with django_capture_on_commit_callbacks(execute=True):
                sync_pending_refunds(gateway=gateway)

        tx.refresh_from_db()
        assert tx.refund_status == RefundStatus.CANCELED
        task.kiq.assert_not_awaited()

    def test_still_pending_refund_is_left_for_next_tick(self) -> None:
        gateway = FakeGateway()
        tx = _issued_pending(gateway)

        assert sync_pending_refunds(gateway=gateway) == 0

        tx.refresh_from_db()
        assert tx.refund_status == RefundStatus.PENDING

    def test_rejected_lookup_goes_to_manual_review(self) -> None:
        gateway = FakeGateway()
        tx = _issued_pending(gateway)

        def reject(refund_id: str) -> None:
            raise GatewayContractError("возврат не найден")

        gateway.get_refund = reject  # type: ignore[method-assign,assignment]
        sync_pending_refunds(gateway=gateway)

        tx.refresh_from_db()
        assert tx.refund_status == RefundStatus.FAILED
        assert tx.metadata["refund_error"] == "возврат не найден"

    def test_immediately_succeeded_refund_notifies_parent(
        self, django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks
    ) -> None:
        tx = _make_refundable_payment([101])

        with patch("apps.billing.tasks.send_refund_email_task") as task:
            task.kiq = AsyncMock()
            with django_capture_on_commit_callbacks(execute=True):
                issue_pending_refunds(gateway=FakeGateway())

        task.kiq.assert_awaited_once_with(str(tx.pk))


@pytest.mark.django_db
class TestRefundEmail:
    def test_email_states_received_amount(self) -> None:
        tx = _make_refundable_payment([101])
        Transaction.objects.filter(pk=tx.pk).update(
            received_amount=350_000, refund_status=RefundStatus.SUCCEEDED
        )

        send_refund_email_task(str(tx.pk))

        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [tx.parent.email]
        assert "3 500,00 ₽" in mail.outbox[0].body

    def test_no_email_for_unfinished_refund(self) -> None:
        tx = _make_refundable_payment([101])

        send_refund_email_task(str(tx.pk))

        assert mail.outbox == []


class TestGetRefundHttp:
    def _gateway(self, response: httpx.Response) -> YookassaHttpGateway:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert request.url.path == "/v3/refunds/rf-42"
            return response

        return YookassaHttpGateway(_SETTINGS, transport=httpx.MockTransport(handler))

    def test_parses_refund_status(self) -> None:
        gateway = self._gateway(
            httpx.Response(200, json={"id": "rf-42", "status": "canceled"})
        )

        assert gateway.get_refund("rf-42").status == "canceled"

    def test_4xx_is_contract_error(self) -> None:
        gateway = self._gateway(httpx.Response(404))

        with pytest.raises(GatewayContractError):
            gateway.get_refund("rf-42")

    def test_5xx_is_retryable(self) -> None:
        gateway = self._gateway(httpx.Response(503))

        with pytest.raises(GatewayNetworkError):
            gateway.get_refund("rf-42")

    def test_rejects_path_traversal_id(self) -> None:
        gateway = self._gateway(httpx.Response(200))

        with pytest.raises(GatewayContractError):
            gateway.get_refund("../payments")


@pytest.mark.django_db
class TestRefundColumnsMigration:
    def test_moves_refund_fields_out_of_metadata(self) -> None:
        tx = _make_refundable_payment([101])
        Transaction.objects.filter(pk=tx.pk).update(
            metadata={"refund_status": "succeeded", "refund_id": "rf-9", "x": 1}
        )
        migration = import_module(
            "apps.billing.migrations.0010_transaction_refund_status"
        )

        migration._metadata_to_columns(django_apps, None)

        tx.refresh_from_db()
        assert tx.refund_status == RefundStatus.SUCCEEDED
        assert tx.refund_id == "rf-9"
        assert tx.metadata == {"x": 1}
