from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import async_to_sync
from pytest_django.fixtures import DjangoCaptureOnCommitCallbacks

from apps.billing.models import RefundStatus, Transaction
from apps.billing.services import (
    issue_pending_refunds,
    resolve_refund_manually,
    sync_pending_refunds,
)
from apps.billing.tasks import notify_refund_review_task
from apps.billing.tests.test_amount_mismatch_refund import _mismatched
from apps.billing.tests.test_billing import (
    FakeGateway,
    _make_pending_payment,
    _make_refundable_payment,
)


@pytest.fixture
def alert_task() -> Iterator[AsyncMock]:
    # ПОЧЕМУ: подменяется постановка в брокер — проверяем, что задача
    # действительно ушла в очередь после коммита
    with patch("apps.billing.tasks.notify_refund_review_task") as task:
        task.kiq = AsyncMock()
        yield task.kiq


@pytest.mark.django_db
class TestAlertIsScheduled:
    def test_foreign_currency_alerts_managers(
        self,
        alert_task: AsyncMock,
        django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    ) -> None:
        tx = _make_pending_payment([101])

        with django_capture_on_commit_callbacks(execute=True):
            _mismatched(tx, currency="USD")

        alert_task.assert_awaited_once_with(str(tx.pk))

    def test_rejected_refund_alerts_managers(
        self,
        alert_task: AsyncMock,
        django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    ) -> None:
        tx = _make_refundable_payment([101])
        gateway = FakeGateway(failing_refund_ids={f"yk-{tx.pk}"})

        with django_capture_on_commit_callbacks(execute=True):
            issue_pending_refunds(gateway=gateway)

        alert_task.assert_awaited_once_with(str(tx.pk))

    def test_refund_canceled_by_provider_alerts_managers(
        self,
        alert_task: AsyncMock,
        django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    ) -> None:
        tx = _make_refundable_payment([101])
        gateway = FakeGateway(new_refund_status="pending")
        issue_pending_refunds(gateway=gateway)
        gateway.refund_outcomes["rf-1"] = "canceled"

        with django_capture_on_commit_callbacks(execute=True):
            sync_pending_refunds(gateway=gateway)

        alert_task.assert_awaited_once_with(str(tx.pk))

    def test_regular_refund_does_not_alert(
        self,
        alert_task: AsyncMock,
        django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    ) -> None:
        # ПОЧЕМУ: обычный возврат проходит сам — менеджеров не дёргаем
        tx = _make_pending_payment([101])

        # ПОЧЕМУ: успешный возврат ставит письмо родителю — его тоже подменяем,
        # иначе in-memory брокер выполнит задачу в своём потоке со своим
        # подключением к тестовой БД
        with (
            patch("apps.billing.tasks.send_refund_email_task") as email_task,
            django_capture_on_commit_callbacks(execute=True),
        ):
            email_task.kiq = AsyncMock()
            gateway = _mismatched(tx, amount_kopecks=100)
            issue_pending_refunds(gateway=gateway)

        email_task.kiq.assert_awaited_once_with(str(tx.pk))
        alert_task.assert_not_awaited()


def _awaiting_manual_review() -> Transaction:
    tx = _make_pending_payment([101])
    _mismatched(tx, currency="USD")
    tx.refresh_from_db()
    return tx


@pytest.mark.django_db
class TestAlertMessage:
    def test_message_has_amount_reason_and_contacts(self) -> None:
        tx = _awaiting_manual_review()

        with patch("apps.billing.tasks.send_manager_message", new=AsyncMock()) as send:
            async_to_sync(notify_refund_review_task.original_func)(str(tx.pk))

        text = send.await_args.args[0]
        assert "7 000,00 ₽" in text
        assert "USD" in text
        assert tx.parent.email in text
        assert f"yk-{tx.pk}" in text

    def test_already_resolved_review_is_not_sent(self) -> None:
        tx = _awaiting_manual_review()
        resolve_refund_manually(tx.pk, resolved_by="manager")

        with patch("apps.billing.tasks.send_manager_message", new=AsyncMock()) as send:
            async_to_sync(notify_refund_review_task.original_func)(str(tx.pk))

        send.assert_not_awaited()

    def test_refund_in_queue_is_not_sent(self) -> None:
        tx = _make_refundable_payment([101])
        assert tx.refund_status != RefundStatus.FAILED

        with patch("apps.billing.tasks.send_manager_message", new=AsyncMock()) as send:
            async_to_sync(notify_refund_review_task.original_func)(str(tx.pk))

        send.assert_not_awaited()
