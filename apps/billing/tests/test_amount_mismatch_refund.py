from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpRequest
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.billing.admin import TransactionAdmin
from apps.billing.models import ParentDeposit, Transaction, TransactionStatus
from apps.billing.services import (
    AmountMismatchError,
    PaymentSucceededAfterExpiryError,
    RefundNotAwaitingManualError,
    confirm_payment,
    issue_pending_refunds,
    resolve_refund_manually,
    sweep_stale_pending_transactions,
)
from apps.billing.tests.test_billing import (
    FakeGateway,
    FakeSchedulePort,
    ParentDepositFactory,
    ParentFactory,
    _checkout,
    _gateway_for,
    _make_pending_payment,
)


def _mismatched(
    tx: Transaction, *, amount_kopecks: int | None = None, currency: str = "RUB"
) -> FakeGateway:
    payment_id, gateway = _gateway_for(
        tx, "succeeded", amount_kopecks=amount_kopecks, currency=currency
    )
    with pytest.raises(AmountMismatchError):
        confirm_payment(
            payment_id=payment_id, gateway=gateway, schedule_port=FakeSchedulePort()
        )
    return gateway


@pytest.mark.django_db
class TestAmountMismatchRefund:
    def test_underpayment_is_queued_for_refund(self) -> None:
        # Проблема 1: деньги пришли, заказ отменён — возврат обязан встать в очередь
        tx = _make_pending_payment([101])
        payment_id, gateway = _gateway_for(tx, "succeeded", amount_kopecks=100)

        with pytest.raises(AmountMismatchError):
            confirm_payment(
                payment_id=payment_id, gateway=gateway, schedule_port=FakeSchedulePort()
            )

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.FAILED
        assert tx.requires_compensation is True

    def test_mismatch_refunds_received_amount_and_returns_deposit(self) -> None:
        # Оплата частично с депозита: депозитная часть возвращается на депозит,
        # карточная — возвратом ЮКассы ровно на пришедшую сумму
        parent = ParentFactory()
        ParentDepositFactory(parent=parent, balance=200_000)
        before = set(Transaction.objects.values_list("pk", flat=True))
        _checkout([101], parent=parent, use_deposit=True)
        tx = Transaction.objects.exclude(pk__in=before).get()
        assert tx.amount == 500_000
        payment_id, gateway = _gateway_for(tx, "succeeded", amount_kopecks=100)

        with pytest.raises(AmountMismatchError):
            confirm_payment(
                payment_id=payment_id, gateway=gateway, schedule_port=FakeSchedulePort()
            )
        issue_pending_refunds(gateway=gateway)

        assert ParentDeposit.objects.get(parent=parent).balance == 200_000
        assert gateway.refund_calls == [(payment_id, 100, f"refund-{tx.pk}")]

    def test_end_to_end_refund_is_issued_once_with_received_amount(self) -> None:
        # Сквозной: чекаут → вебхук с другой суммой → к возврату → возврат
        # на фактическую сумму → повторы вебхука и обработчика ничего не добавляют
        before = set(Transaction.objects.values_list("pk", flat=True))
        _checkout([101])
        tx = Transaction.objects.exclude(pk__in=before).get()
        gateway = _mismatched(tx, amount_kopecks=350_000)

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.FAILED
        assert tx.received_amount == 350_000
        assert tx.requires_compensation is True

        assert issue_pending_refunds(gateway=gateway) == 1
        confirm_payment(
            payment_id=f"yk-{tx.pk}",
            gateway=gateway,
            schedule_port=FakeSchedulePort(),
        )
        assert issue_pending_refunds(gateway=gateway) == 0

        tx.refresh_from_db()
        assert gateway.refund_calls == [(f"yk-{tx.pk}", 350_000, f"refund-{tx.pk}")]
        assert tx.requires_compensation is False
        assert tx.refund_status == "SUCCEEDED"

    def test_overpayment_is_refunded_in_full(self) -> None:
        # Решение бизнеса: пришло больше — заказ отменяем, возвращаем всё
        tx = _make_pending_payment([101])
        gateway = _mismatched(tx, amount_kopecks=900_000)

        issue_pending_refunds(gateway=gateway)

        assert gateway.refund_calls == [(f"yk-{tx.pk}", 900_000, f"refund-{tx.pk}")]

    def test_foreign_currency_goes_to_manual_review_not_gateway(self) -> None:
        tx = _make_pending_payment([101])
        gateway = _mismatched(tx, currency="USD")

        assert issue_pending_refunds(gateway=gateway) == 0

        tx.refresh_from_db()
        assert gateway.refund_calls == []
        assert tx.requires_compensation is False
        assert tx.refund_status == "FAILED"
        assert "USD" in tx.metadata["refund_error"]


@pytest.mark.django_db
class TestLatePaymentRefundAmount:
    def _swept(self) -> Transaction:
        tx = _make_pending_payment([101])
        Transaction.objects.filter(pk=tx.pk).update(
            created_at=timezone.now() - timedelta(hours=1)
        )
        assert sweep_stale_pending_transactions() == 1
        return tx

    def test_late_payment_refunds_received_not_expected_amount(self) -> None:
        # Проблема 2: возврат на tx.amount, хотя пришло меньше — ЮКасса отвергнет
        tx = self._swept()
        payment_id, gateway = _gateway_for(tx, "succeeded", amount_kopecks=100)

        with pytest.raises(PaymentSucceededAfterExpiryError):
            confirm_payment(
                payment_id=payment_id, gateway=gateway, schedule_port=FakeSchedulePort()
            )
        issue_pending_refunds(gateway=gateway)

        assert gateway.refund_calls == [(payment_id, 100, f"refund-{tx.pk}")]

    def test_repeated_webhook_after_refund_does_not_requeue(self) -> None:
        # Попутная находка: после возврата флаг в metadata сброшен в False,
        # и повторный вебхук succeeded снова ставит возврат в очередь
        tx = self._swept()
        payment_id, gateway = _gateway_for(tx, "succeeded")
        with pytest.raises(PaymentSucceededAfterExpiryError):
            confirm_payment(
                payment_id=payment_id, gateway=gateway, schedule_port=FakeSchedulePort()
            )
        assert issue_pending_refunds(gateway=gateway) == 1

        confirm_payment(
            payment_id=payment_id, gateway=gateway, schedule_port=FakeSchedulePort()
        )

        tx.refresh_from_db()
        assert tx.requires_compensation is False
        assert tx.refund_status == "SUCCEEDED"


def _awaiting_manual() -> Transaction:
    tx = _make_pending_payment([101])
    _mismatched(tx, currency="USD")
    tx.refresh_from_db()
    return tx


@pytest.mark.django_db
class TestResolveRefundManually:
    def test_closes_manual_review_and_keeps_audit(self) -> None:
        tx = _awaiting_manual()

        resolve_refund_manually(tx.pk, resolved_by="manager")

        tx.refresh_from_db()
        assert tx.refund_status == "MANUAL"
        assert tx.metadata["refund_resolved_by"] == "manager"
        assert "USD" in tx.metadata["refund_error"]

    def test_rejects_refund_still_in_queue(self) -> None:
        # ПОЧЕМУ: иначе менеджер «закроет» возврат, который автомат ещё выплатит
        tx = _make_pending_payment([101])
        _mismatched(tx, amount_kopecks=100)

        with pytest.raises(RefundNotAwaitingManualError):
            resolve_refund_manually(tx.pk, resolved_by="manager")

    def test_rejects_second_close(self) -> None:
        tx = _awaiting_manual()
        resolve_refund_manually(tx.pk, resolved_by="manager")

        with pytest.raises(RefundNotAwaitingManualError):
            resolve_refund_manually(tx.pk, resolved_by="manager")


def _admin_request(rf: RequestFactory, user: object) -> HttpRequest:
    request = rf.get("/")
    request.user = user  # type: ignore[assignment]
    SessionMiddleware(lambda r: None).process_request(request)  # type: ignore[arg-type]
    request.session.save()
    request._messages = FallbackStorage(request)  # noqa: SLF001
    return request


@pytest.mark.django_db
class TestTransactionAdmin:
    def test_manual_filter_lists_only_awaiting_review(
        self, admin_client: Client
    ) -> None:
        manual = _awaiting_manual()
        queued = _make_pending_payment([102])
        _mismatched(queued, amount_kopecks=100)
        url = reverse("admin:billing_transaction_changelist")

        response = admin_client.get(url, {"refund": "manual"})

        assert response.status_code == 200
        body = response.content.decode()
        assert f"yk-{manual.pk}" in body
        assert f"yk-{queued.pk}" not in body

    def test_row_action_resolves_refund(
        self, admin_user: AbstractBaseUser, rf: RequestFactory
    ) -> None:
        tx = _awaiting_manual()
        model_admin = TransactionAdmin(Transaction, AdminSite())
        request = _admin_request(rf, admin_user)

        response = model_admin.row_resolve_refund(request, str(tx.pk))

        tx.refresh_from_db()
        assert response.status_code == 302
        assert tx.refund_status == "MANUAL"
        assert tx.metadata["refund_resolved_by"] == admin_user.get_username()
