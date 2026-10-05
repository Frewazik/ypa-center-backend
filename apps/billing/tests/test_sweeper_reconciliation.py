from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from threading import Barrier, Thread
from typing import Any

import pytest
from django.db import connection
from django.utils import timezone

from apps.billing import services as billing_services
from apps.billing import tasks as billing_tasks
from apps.billing.adapters import (
    GatewayContractError,
    GatewayNetworkError,
    PaymentGateway,
    PaymentInfo,
)
from apps.billing.models import (
    Enrollment,
    EnrollmentStatus,
    Subscription,
    SubscriptionSlot,
    SubscriptionStatus,
    Transaction,
    TransactionStatus,
)
from apps.billing.services import (
    PaymentSucceededAfterExpiryError,
    confirm_payment,
    issue_pending_refunds,
    sweep_stale_pending_transactions,
)
from apps.billing.tests.test_billing import (
    FakeGateway,
    FakeSchedulePort,
    RaisingGateway,
    _gateway_for,
    _make_pending_payment,
)
from apps.events.ports import DjangoEventBookingPort


def _stale_order(
    slot_id: int = 101, *, age: timedelta = timedelta(minutes=20)
) -> Transaction:
    tx = _make_pending_payment([slot_id])
    Transaction.objects.filter(pk=tx.pk).update(created_at=timezone.now() - age)
    tx.refresh_from_db()
    return tx


def _sweep(gateway: PaymentGateway, *, later: timedelta = timedelta(0)) -> int:
    # ПОЧЕМУ later: следующий тик крона — сдвиг «сейчас» вперёд
    return sweep_stale_pending_transactions(
        gateway=gateway,
        schedule_port=FakeSchedulePort(),
        now=timezone.now() + later,
        event_port=DjangoEventBookingPort(),
    )


def _critical_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.CRITICAL]


@dataclass
class CountingGateway(FakeGateway):
    # ПОЧЕМУ: считает походы в ЮКассу — лимит тика и остановка сверки
    # при недоступном провайдере проверяются по числу сетевых вызовов
    get_calls: list[str] = field(default_factory=list)

    def get_payment(self, payment_id: str) -> PaymentInfo:
        self.get_calls.append(payment_id)
        return super().get_payment(payment_id)


@dataclass
class CountingRaisingGateway(RaisingGateway):
    get_calls: list[str] = field(default_factory=list)

    def get_payment(self, payment_id: str) -> PaymentInfo:
        self.get_calls.append(payment_id)
        return super().get_payment(payment_id)


@pytest.mark.django_db
class TestLostWebhookReconciliation:
    def test_paid_order_without_webhook_is_not_lost_by_ttl_sweeper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Родитель оплатил, вебхук не дошёл, через 15 минут крон свипера снял
        # заказ. Деньги у нас — значит, либо абонемент, либо возврат
        tx = _make_pending_payment([101])
        Transaction.objects.filter(pk=tx.pk).update(
            created_at=timezone.now() - timedelta(minutes=20)
        )
        payment_id, gateway = _gateway_for(tx, "succeeded")
        # ПОЧЕМУ через задачи: проверяем путь крона целиком, подменяя только
        # сетевую границу — клиент ЮКассы, который задачи создают сами
        monkeypatch.setattr(billing_tasks, "YookassaHttpGateway", lambda: gateway)
        monkeypatch.setattr(billing_tasks, "resolve_schedule_port", FakeSchedulePort)

        billing_tasks.sweep_billing_states()
        billing_tasks.process_compensation_refunds()

        tx.refresh_from_db()
        subscription_active = (
            Subscription.objects.get(pk=tx.subscription_id).status
            == SubscriptionStatus.ACTIVE
        )
        refunded = [call[0] for call in gateway.refund_calls] == [payment_id]
        assert subscription_active or refunded, (
            f"деньги застряли: транзакция {tx.status}, "
            f"metadata={tx.metadata}, возвратов={gateway.refund_calls}"
        )
        assert tx.status != TransactionStatus.PENDING


@pytest.mark.django_db
class TestReconciliationOutcomes:
    def test_succeeded_is_applied_like_webhook_and_alerts(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        tx = _stale_order()
        payment_id, gateway = _gateway_for(tx, "succeeded")

        with caplog.at_level(logging.CRITICAL, logger="apps.billing.services"):
            swept = _sweep(gateway)

        tx.refresh_from_db()
        assert swept == 0  # не снят по TTL — проведён
        assert tx.status == TransactionStatus.SUCCEEDED
        assert tx.external_id == payment_id
        assert tx.requires_compensation is False
        assert (
            Subscription.objects.get(pk=tx.subscription_id).status
            == SubscriptionStatus.ACTIVE
        )
        assert (
            SubscriptionSlot.objects.filter(subscription_id=tx.subscription_id).count()
            == 1
        )
        assert gateway.refund_calls == []
        # ПОЧЕМУ: оплата без вебхука — сигнал, что вебхуки не доходят
        assert any("вебхук не пришёл" in m for m in _critical_messages(caplog))

    def test_canceled_at_provider_is_applied_like_webhook(self) -> None:
        tx = _stale_order()
        _, gateway = _gateway_for(tx, "canceled")

        assert _sweep(gateway) == 0

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.CANCELED
        # ПОЧЕМУ: отмена от провайдера окончательна — не TTL, поздний успех невозможен
        assert "canceled_reason" not in tx.metadata
        assert (
            Enrollment.objects.get(subscription_id=tx.subscription_id).status
            == EnrollmentStatus.CANCELED
        )
        assert (
            Subscription.objects.get(pk=tx.subscription_id).status
            == SubscriptionStatus.CANCELED
        )

    def test_still_pending_is_expired_and_late_payment_is_refunded(self) -> None:
        tx = _stale_order()
        _, pending_gateway = _gateway_for(tx, "pending")

        assert _sweep(pending_gateway) == 1

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.CANCELED
        assert tx.metadata["canceled_reason"] == "TTL_EXPIRED"
        assert (
            Enrollment.objects.get(subscription_id=tx.subscription_id).status
            == EnrollmentStatus.CANCELED
        )

        # Родитель всё же оплатил по старой ссылке, вебхук дошёл — возврат
        payment_id, paid_gateway = _gateway_for(tx, "succeeded")
        with pytest.raises(PaymentSucceededAfterExpiryError):
            confirm_payment(
                payment_id=payment_id,
                gateway=paid_gateway,
                schedule_port=FakeSchedulePort(),
                event_port=DjangoEventBookingPort(),
            )
        assert issue_pending_refunds(gateway=paid_gateway) == 1
        assert paid_gateway.refund_calls == [(payment_id, tx.amount, f"refund-{tx.pk}")]

    def test_gateway_down_keeps_order_and_stops_calls_for_the_tick(self) -> None:
        first = _stale_order(101)
        second = _stale_order(102)
        gateway = CountingRaisingGateway(error_factory=GatewayNetworkError)

        assert _sweep(gateway) == 0

        first.refresh_from_db()
        second.refresh_from_db()
        # ПОЧЕМУ: отмена вслепую потеряла бы оплату без вебхука — ждём тика
        assert first.status == TransactionStatus.PENDING
        assert second.status == TransactionStatus.PENDING
        assert Enrollment.objects.filter(status=EnrollmentStatus.HELD).count() == 2
        # ЮКасса лежит — второй заказ не тратит ещё один таймаут
        assert len(gateway.get_calls) == 1

    def test_contract_error_also_postpones_reconciliation(self) -> None:
        tx = _stale_order()
        gateway = CountingRaisingGateway(error_factory=GatewayContractError)

        assert _sweep(gateway) == 0

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.PENDING

    def test_gives_up_after_limit_and_expires_blindly_with_alert(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        tx = _stale_order(age=timedelta(hours=3))
        gateway = CountingRaisingGateway(error_factory=GatewayNetworkError)

        with caplog.at_level(logging.CRITICAL, logger="apps.billing.services"):
            assert _sweep(gateway) == 1

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.CANCELED
        assert tx.metadata["canceled_reason"] == "TTL_EXPIRED"
        assert gateway.get_calls == []  # после лимита в ЮКассу не ходим
        assert any("без сверки" in m for m in _critical_messages(caplog))

    def test_unknown_payment_is_expired(self) -> None:
        tx = _stale_order()

        assert _sweep(FakeGateway()) == 1  # ЮКасса отвечает 404

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.CANCELED
        assert tx.metadata["canceled_reason"] == "TTL_EXPIRED"

    def test_payment_linked_to_other_transaction_is_not_applied(self) -> None:
        tx = _stale_order(101)
        other = _stale_order(102)
        payment_id = tx.external_id
        assert payment_id is not None
        gateway = FakeGateway(
            payments={
                payment_id: PaymentInfo(
                    id=payment_id,
                    status="succeeded",
                    transaction_id=str(other.pk),
                    amount_kopecks=other.amount,
                    currency="RUB",
                )
            }
        )

        _sweep(gateway)

        tx.refresh_from_db()
        other.refresh_from_db()
        assert tx.status == TransactionStatus.CANCELED
        # ПОЧЕМУ: чужой платёж не проводится сверкой нашей транзакции
        assert other.status != TransactionStatus.SUCCEEDED

    def test_order_without_external_id_is_expired_without_asking(self) -> None:
        tx = _stale_order()
        Transaction.objects.filter(pk=tx.pk).update(external_id=None)
        gateway = CountingGateway()

        assert _sweep(gateway) == 1

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.CANCELED
        assert gateway.get_calls == []

    def test_reconciliation_budget_per_tick(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(billing_services, "_RECONCILE_CHUNK_SIZE", 1)
        first = _stale_order(101)
        second = _stale_order(102)
        unlinked = _stale_order(103)
        Transaction.objects.filter(pk=unlinked.pk).update(external_id=None)
        gateway = CountingGateway(
            payments={
                **_gateway_for(first, "pending")[1].payments,
                **_gateway_for(second, "pending")[1].payments,
            }
        )

        assert _sweep(gateway) == 2  # один сверенный + один без external_id
        assert len(gateway.get_calls) == 1
        assert _sweep(gateway) == 1
        assert len(gateway.get_calls) == 2
        assert not Transaction.objects.filter(status=TransactionStatus.PENDING).exists()

    def test_sweep_task_survives_gateway_outage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ПОЧЕМУ: сбой ЮКассы не должен ронять задачу — следом в ней идёт
        # истечение абонементов, которое от провайдера не зависит
        _stale_order()
        monkeypatch.setattr(
            billing_tasks,
            "YookassaHttpGateway",
            lambda: RaisingGateway(error_factory=GatewayNetworkError),
        )
        monkeypatch.setattr(billing_tasks, "resolve_schedule_port", FakeSchedulePort)

        billing_tasks.sweep_billing_states()

        assert Transaction.objects.get().status == TransactionStatus.PENDING


@pytest.mark.django_db
class TestReconciliationWebhookInterleaving:
    def test_webhook_between_gateway_answer_and_write_is_not_doubled(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Детерминированная гонка: вебхук проводит платёж ровно в окне между
        # ответом ЮКассы свиперу и записью свипера в БД
        tx = _stale_order()
        payment_id, base = _gateway_for(tx, "succeeded")

        class WebhookInTheMiddle(FakeGateway):
            def get_payment(self, requested_id: str) -> PaymentInfo:
                info = base.get_payment(requested_id)
                confirm_payment(
                    payment_id=requested_id,
                    gateway=base,
                    schedule_port=FakeSchedulePort(),
                    event_port=DjangoEventBookingPort(),
                )
                return info

        with caplog.at_level(logging.CRITICAL, logger="apps.billing.services"):
            assert _sweep(WebhookInTheMiddle()) == 0

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.SUCCEEDED
        assert tx.requires_compensation is False
        assert (
            SubscriptionSlot.objects.filter(subscription_id=tx.subscription_id).count()
            == 1
        )
        # ПОЧЕМУ: вебхук дошёл — тревога «вебхук не пришёл» была бы ложной
        assert _critical_messages(caplog) == []


@pytest.mark.django_db(transaction=True)
class TestReconciliationWebhookRace:
    def test_concurrent_webhook_and_sweeper_apply_payment_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tx = _stale_order()
        payment_id, gateway = _gateway_for(tx, "succeeded")
        # ПОЧЕМУ Barrier перед _apply_success: оба потока уже получили ответ
        # ЮКассы и прошли все предварительные проверки, в запись входят
        # одновременно — сериализует их только FOR UPDATE по строке транзакции
        barrier = Barrier(2, timeout=10)
        original_apply_success = billing_services._apply_success

        def synced_apply_success(*args: Any, **kwargs: Any) -> None:
            barrier.wait()
            original_apply_success(*args, **kwargs)

        monkeypatch.setattr(billing_services, "_apply_success", synced_apply_success)
        errors: list[BaseException] = []

        def run(target: object) -> None:
            try:
                target()  # type: ignore[operator]
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connection.close()

        webhook = Thread(
            target=run,
            args=(
                lambda: confirm_payment(
                    payment_id=payment_id,
                    gateway=gateway,
                    schedule_port=FakeSchedulePort(),
                    event_port=DjangoEventBookingPort(),
                ),
            ),
        )
        sweeper = Thread(target=run, args=(lambda: _sweep(gateway),))
        webhook.start()
        sweeper.start()
        webhook.join(timeout=20)
        sweeper.join(timeout=20)

        assert not webhook.is_alive() and not sweeper.is_alive()
        assert errors == []
        tx.refresh_from_db()
        assert tx.status == TransactionStatus.SUCCEEDED
        assert tx.requires_compensation is False
        assert (
            Subscription.objects.get(pk=tx.subscription_id).status
            == SubscriptionStatus.ACTIVE
        )
        # Второй проход не добавил фишек и не завёл компенсацию
        assert (
            SubscriptionSlot.objects.filter(subscription_id=tx.subscription_id).count()
            == 1
        )
        assert gateway.refund_calls == []


_NEXT_TICK = timedelta(minutes=5)


def _expired_unpaid(slot_id: int = 101) -> Transaction:
    # Родитель не оплатил за TTL: сверка видит pending и снимает заказ
    tx = _stale_order(slot_id)
    _, pending = _gateway_for(tx, "pending")
    assert _sweep(pending) == 1
    tx.refresh_from_db()
    assert tx.status == TransactionStatus.CANCELED
    return tx


@pytest.mark.django_db
class TestPostExpiryRecheck:
    def test_expired_order_with_payment_is_queued_for_recheck(self) -> None:
        tx = _stale_order()
        gateway = CountingGateway(payments=_gateway_for(tx, "pending")[1].payments)

        assert _sweep(gateway) == 1

        tx.refresh_from_db()
        assert tx.payment_recheck_until is not None
        # ПОЧЕМУ: только что спрошенный заказ досверка в том же тике не трогает
        assert len(gateway.get_calls) == 1

    def test_payment_after_expiry_without_webhook_is_refunded(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Оплатил на 16-й минуте, вебхук потерян — досверка ставит возврат
        tx = _expired_unpaid()
        payment_id, paid = _gateway_for(tx, "succeeded")

        with caplog.at_level(logging.CRITICAL, logger="apps.billing.services"):
            _sweep(paid, later=_NEXT_TICK)

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.CANCELED
        assert tx.requires_compensation is True
        assert tx.metadata["reason"] == "PAYMENT_SUCCEEDED_AFTER_EXPIRY"
        assert tx.payment_recheck_until is None
        assert any("оплачен после снятия" in m for m in _critical_messages(caplog))
        assert issue_pending_refunds(gateway=paid) == 1
        assert paid.refund_calls == [(payment_id, tx.amount, f"refund-{tx.pk}")]

    def test_still_pending_stays_in_queue(self) -> None:
        tx = _expired_unpaid()
        _, pending = _gateway_for(tx, "pending")

        _sweep(pending, later=_NEXT_TICK)

        tx.refresh_from_db()
        assert tx.payment_recheck_until is not None
        assert tx.requires_compensation is False

    def test_canceled_by_provider_closes_recheck(self) -> None:
        tx = _expired_unpaid()
        _, canceled = _gateway_for(tx, "canceled")

        _sweep(canceled, later=_NEXT_TICK)

        tx.refresh_from_db()
        assert tx.payment_recheck_until is None
        assert tx.requires_compensation is False

    def test_refund_already_paid_by_webhook_path_is_not_queued_again(self) -> None:
        # Вебхук пришёл, возврат выплачен (флаг сброшен) — досверка не должна
        # поставить второй возврат
        tx = _expired_unpaid()
        payment_id, paid = _gateway_for(tx, "succeeded")
        with pytest.raises(PaymentSucceededAfterExpiryError):
            confirm_payment(
                payment_id=payment_id,
                gateway=paid,
                schedule_port=FakeSchedulePort(),
                event_port=DjangoEventBookingPort(),
            )
        assert issue_pending_refunds(gateway=paid) == 1

        _sweep(paid, later=_NEXT_TICK)

        tx.refresh_from_db()
        assert tx.payment_recheck_until is None
        assert tx.requires_compensation is False
        assert issue_pending_refunds(gateway=paid) == 0
        assert len(paid.refund_calls) == 1

    def test_manual_review_by_webhook_is_not_reported_as_lost_webhook(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Вебхук дошёл, но оплата не в рублях — ушла на ручной разбор без
        # постановки в очередь. Досверка обязана узнать учтённую оплату
        # по received_amount и не кричать «вебхук не пришёл»
        tx = _expired_unpaid()
        payment_id, paid = _gateway_for(tx, "succeeded", currency="USD")
        with pytest.raises(PaymentSucceededAfterExpiryError):
            confirm_payment(
                payment_id=payment_id,
                gateway=paid,
                schedule_port=FakeSchedulePort(),
                event_port=DjangoEventBookingPort(),
            )

        with caplog.at_level(logging.CRITICAL, logger="apps.billing.services"):
            _sweep(paid, later=_NEXT_TICK)

        tx.refresh_from_db()
        assert tx.payment_recheck_until is None
        assert tx.requires_compensation is False
        assert _critical_messages(caplog) == []

    def test_webhook_between_recheck_answer_and_write_is_not_doubled(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        tx = _expired_unpaid()
        payment_id, base = _gateway_for(tx, "succeeded")

        class WebhookInTheMiddle(FakeGateway):
            def get_payment(self, requested_id: str) -> PaymentInfo:
                info = base.get_payment(requested_id)
                with pytest.raises(PaymentSucceededAfterExpiryError):
                    confirm_payment(
                        payment_id=requested_id,
                        gateway=base,
                        schedule_port=FakeSchedulePort(),
                        event_port=DjangoEventBookingPort(),
                    )
                return info

        with caplog.at_level(logging.CRITICAL, logger="apps.billing.services"):
            _sweep(WebhookInTheMiddle(), later=_NEXT_TICK)

        tx.refresh_from_db()
        assert tx.requires_compensation is True
        assert tx.payment_recheck_until is None
        # ПОЧЕМУ: вебхук дошёл — тревога «вебхук не пришёл» была бы ложной
        assert _critical_messages(caplog) == []

    def test_overdue_recheck_is_closed_with_alert_without_asking(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        tx = _expired_unpaid()
        gateway = CountingGateway(payments=_gateway_for(tx, "pending")[1].payments)

        with caplog.at_level(logging.CRITICAL, logger="apps.billing.services"):
            _sweep(gateway, later=timedelta(hours=25))

        tx.refresh_from_db()
        assert tx.payment_recheck_until is None
        assert gateway.get_calls == []
        assert any("окончательного статуса" in m for m in _critical_messages(caplog))

    def test_gateway_down_keeps_queue_and_stops_calls(self) -> None:
        first = _stale_order(101)
        second = _stale_order(102)
        pending = FakeGateway(
            payments={
                **_gateway_for(first, "pending")[1].payments,
                **_gateway_for(second, "pending")[1].payments,
            }
        )
        assert _sweep(pending) == 2
        gateway = CountingRaisingGateway(error_factory=GatewayNetworkError)

        _sweep(gateway, later=_NEXT_TICK)

        first.refresh_from_db()
        second.refresh_from_db()
        assert first.payment_recheck_until is not None
        assert second.payment_recheck_until is not None
        assert len(gateway.get_calls) == 1

    def test_order_without_payment_is_not_queued(self) -> None:
        tx = _stale_order()
        Transaction.objects.filter(pk=tx.pk).update(external_id=None)

        assert _sweep(CountingGateway()) == 1

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.CANCELED
        assert tx.payment_recheck_until is None

    def test_recheck_uses_budget_left_after_pending_sweep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ПОЧЕМУ: снятие по TTL держит места — оно тратит бюджет первым
        expired = _expired_unpaid(101)
        monkeypatch.setattr(billing_services, "_RECONCILE_CHUNK_SIZE", 1)
        fresh = _stale_order(102)
        gateway = CountingGateway(
            payments={
                **_gateway_for(expired, "pending")[1].payments,
                **_gateway_for(fresh, "pending")[1].payments,
            }
        )

        _sweep(gateway, later=_NEXT_TICK)

        assert gateway.get_calls == [fresh.external_id]
