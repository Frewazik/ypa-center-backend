# ПОЧЕМУ: обработка вебхука вынесена в фон для быстрого ответа провайдеру,
# перманентные ошибки гасятся внутри, ретрай допускается только для сетевых сбоев

from __future__ import annotations

import logging

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from apps.core.telegram import send_manager_message
from config.tkq import broker

from apps.billing.adapters import (
    GatewayContractError,
    InvalidPaymentIdError,
    PaymentGateway,
    PaymentNotFoundError,
    YookassaHttpGateway,
)
from apps.billing.models import RefundStatus, Transaction
from apps.billing.ports import (
    EventBookingPort,
    SchedulePort,
    resolve_event_port,
    resolve_schedule_port,
)
from apps.billing.services import (
    CANCELED_BY_CENTER_REASON,
    REFUND_STATUSES_AWAITING_MANUAL,
    BillingError,
    UnlinkedPaymentError,
    confirm_payment,
    issue_pending_refunds,
    sweep_expired_subscriptions,
    sweep_finalized_idempotency_records,
    sweep_stale_pending_transactions,
    sync_pending_refunds,
)

logger = logging.getLogger(__name__)


def run_payment_verification(
    payment_id: str,
    gateway: PaymentGateway,
    schedule_port: SchedulePort,
    event_port: EventBookingPort,
) -> None:
    # ПОЧЕМУ: логика вынесена из @broker.task в чистую функцию
    # для изоляции бизнес-логики и удобства тестирования без поднятия брокера
    try:
        confirm_payment(
            payment_id=payment_id,
            gateway=gateway,
            schedule_port=schedule_port,
            event_port=event_port,
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
    run_payment_verification(
        payment_id,
        YookassaHttpGateway(),
        resolve_schedule_port(),
        resolve_event_port(),
    )


@broker.task(schedule=[{"cron": "*/5 * * * *"}])
def sweep_billing_states() -> None:
    # ПОЧЕМУ: операция абсолютно идемпотентна, настройка ретраев не требуется,
    # в случае сбоя стейт будет консистентно починен в следующий тик крона.
    # Сбои ЮКассы при сверке гасятся внутри свипера — истечение абонементов
    # ниже не зависит от доступности провайдера
    canceled = sweep_stale_pending_transactions(
        gateway=YookassaHttpGateway(),
        schedule_port=resolve_schedule_port(),
        event_port=resolve_event_port(),
    )
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
    gateway = YookassaHttpGateway()
    issued = issue_pending_refunds(gateway=gateway)
    settled = sync_pending_refunds(gateway=gateway)
    if issued or settled:
        logger.info(
            "Компенсации: создано возвратов — %d, получен итог по возвратам — %d.",
            issued,
            settled,
        )


@broker.task(retry_on_error=True, max_retries=3)
def send_refund_email_task(transaction_id: str) -> None:
    # ПОЧЕМУ: синхронная функция уводит SMTP I/O в тредпул.
    # Ставится строго из on_commit (services._schedule_refund_email)
    tx = (
        Transaction.objects.select_related("parent")
        .filter(pk=transaction_id, refund_status=RefundStatus.SUCCEEDED)
        .first()
    )
    if tx is None:
        logger.error("Письмо о возврате: транзакция %s не найдена.", transaction_id)
        return
    amount = _format_rubles(
        tx.received_amount if tx.received_amount is not None else tx.amount
    )
    if tx.event_registration_id is not None:
        # ПОЧЕМУ контакты из брони: гость платит без аккаунта, а email в
        # платной брони обязателен
        event_port = resolve_event_port()
        contacts = event_port.get_contacts(tx.event_registration_id)
        summary = event_port.get_summary(tx.event_registration_id)
        starts = timezone.localtime(summary.starts_at).strftime("%d.%m.%Y в %H:%M")
        what = (
            "отменена"
            if tx.metadata.get("reason") == CANCELED_BY_CENTER_REASON
            else "не была оформлена"
        )
        name, email = contacts.parent_name, contacts.email
        reason = f"Запись на «{summary.title}» {starts} {what}"
    else:
        parent = tx.parent
        if parent is None:
            raise UnlinkedPaymentError(
                str(tx.pk), "у транзакции нет ни родителя, ни брони"
            )
        paid_on = timezone.localdate(tx.created_at).strftime("%d.%m.%Y")
        name, email = parent.full_name, parent.email
        reason = f"Заказ по вашему платежу от {paid_on} не был оформлен"
    send_mail(
        subject="Возврат оплаты — «Улица Радости»",
        message=(
            f"Здравствуйте, {name}!\n\n"
            f"{reason}, поэтому мы вернули {amount}.\n"
            f"Деньги вернутся тем же способом, которым вы платили; срок "
            f"зачисления зависит от банка.\n\n"
            f"Если остались вопросы — свяжитесь с нами."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=False,
    )


@broker.task(retry_on_error=True, max_retries=5)
async def notify_refund_review_task(transaction_id: str) -> None:
    # ПОЧЕМУ: ставится строго из on_commit (services._schedule_refund_review_alert).
    # Сбой Telegram (TelegramDeliveryError) уходит в ретрай брокера
    tx = (
        await Transaction.objects.select_related("parent")
        .filter(
            pk=transaction_id,
            requires_compensation=False,
            refund_status__in=REFUND_STATUSES_AWAITING_MANUAL,
        )
        .afirst()
    )
    if tx is None:
        # ПОЧЕМУ: менеджер мог закрыть возврат раньше, чем дошла очередь
        logger.info("Ручной разбор %s: уже не требуется.", transaction_id)
        return
    amount = tx.received_amount if tx.received_amount is not None else tx.amount
    if tx.event_registration_id is not None:
        contacts = await sync_to_async(resolve_event_port().get_contacts)(
            tx.event_registration_id
        )
        who = (
            f"Бронь события #{tx.event_registration_id}: "
            f"{contacts.parent_name or 'без имени'}, "
            f"{contacts.phone or '—'}, {contacts.email or '—'}"
        )
    elif tx.parent is not None:
        parent = tx.parent
        who = (
            f"Родитель: {parent.full_name or 'без имени'}, "
            f"{str(parent.phone or '') or '—'}, {parent.email}"
        )
    else:
        raise UnlinkedPaymentError(str(tx.pk), "у транзакции нет ни родителя, ни брони")
    text = (
        "Возврат требует ручного разбора\n"
        f"Получено: {_format_rubles(amount)}\n"
        f"Причина: {tx.metadata.get('refund_error') or tx.get_refund_status_display()}\n"
        f"{who}\n"
        f"Платёж ЮКассы: {tx.external_id or '—'}\n"
        "Админка → «Транзакции» → фильтр «Возврат: нужен ручной разбор»"
    )
    await send_manager_message(text, context=f"refund_review tx={tx.pk}")


def _format_rubles(kopecks: int) -> str:
    rubles, rest = divmod(kopecks, 100)
    return f"{rubles:,}".replace(",", " ") + f",{rest:02d} ₽"
