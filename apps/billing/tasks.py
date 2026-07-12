# ПОЧЕМУ: обработка вебхука вынесена в фон для быстрого ответа провайдеру,
# перманентные ошибки гасятся внутри, ретрай допускается только для сетевых сбоев

from __future__ import annotations

import logging

from config.tkq import broker

from apps.billing.adapters import (
    GatewayContractError,
    InvalidPaymentIdError,
    PaymentGateway,
    PaymentNotFoundError,
    YookassaHttpGateway,
)
from apps.billing.ports import SchedulePort, resolve_schedule_port
from apps.billing.services import (
    BillingError,
    confirm_payment,
    issue_pending_refunds,
    sweep_expired_subscriptions,
    sweep_finalized_idempotency_records,
    sweep_stale_pending_transactions,
)

logger = logging.getLogger(__name__)


def run_payment_verification(
    payment_id: str, gateway: PaymentGateway, schedule_port: SchedulePort
) -> None:
    # ПОЧЕМУ: логика вынесена из @broker.task в чистую функцию
    # для изоляции бизнес-логики и удобства тестирования без поднятия брокера
    try:
        confirm_payment(
            payment_id=payment_id, gateway=gateway, schedule_port=schedule_port
        )
    except (PaymentNotFoundError, InvalidPaymentIdError):
        logger.warning(
            "Платёж %s: не найден/некорректен у провайдера — вероятно, поддельный "
            "вебхук; ретрай бессмыслен.",
            payment_id,
        )
    except GatewayContractError:
        logger.critical(
            "Платёж %s: нарушение контракта API ЮКассы (схема ответа/4xx) — "
            "перманентный инцидент, ретрай бессмыслен; требуется вмешательство.",
            payment_id,
            exc_info=True,
        )
    except BillingError:
        logger.exception(
            "Платёж %s: постоянная бизнес-ошибка сверки — требуется ручной разбор; "
            "ретрай бессмыслен.",
            payment_id,
        )
    # ПОЧЕМУ: транзитные ошибки (GatewayNetworkError) не перехватываются намеренно,
    # чтобы пробросить их в брокер и инициировать ретрай через SimpleRetryMiddleware


@broker.task(retry_on_error=True, max_retries=5)
def verify_and_process_payment(payment_id: str) -> None:
    # !!!: мы не доверяем payload вебхука из соображений безопасности,
    # актуальный статус всегда запрашивается напрямую из API провайдера
    run_payment_verification(payment_id, YookassaHttpGateway(), resolve_schedule_port())


@broker.task(schedule=[{"cron": "*/5 * * * *"}])
def sweep_billing_states() -> None:
    # ПОЧЕМУ: операция абсолютно идемпотентна, настройка ретраев не требуется,
    # в случае сбоя стейт будет консистентно починен в следующий тик крона
    canceled = sweep_stale_pending_transactions()
    expired = sweep_expired_subscriptions()
    purged_keys = sweep_finalized_idempotency_records()
    if canceled or expired or purged_keys:
        logger.info(
            "Sweeper: транзакций снято по TTL — %d, абонементов истекло — %d, "
            "ключей идемпотентности убрано — %d.",
            canceled,
            expired,
            purged_keys,
        )


@broker.task(schedule=[{"cron": "*/10 * * * *"}], retry_on_error=True, max_retries=5)
def process_compensation_refunds() -> None:
    # !!!: включен автоматический ретрай при сетевых сбоях, повтор безопасен,
    # так как Idempotence-Key провайдера жестко детерминирован ID транзакции
    issued = issue_pending_refunds(gateway=YookassaHttpGateway())
    if issued:
        logger.info("Компенсации: создано возвратов — %d.", issued)
