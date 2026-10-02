from __future__ import annotations

import calendar
import logging
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

from django.core.cache import cache
from django.db import IntegrityError
from django.db import transaction as db_transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.billing.adapters import (
    GatewayContractError,
    GatewayError,
    GatewayNetworkError,
    InvalidPaymentIdError,
    PaymentGateway,
    PaymentInfo,
    PaymentNotFoundError,
)
from apps.billing.models import (
    Attendance,
    AttendanceStatus,
    DepositEntry,
    DepositEntryReason,
    Enrollment,
    EnrollmentStatus,
    EnrollmentType,
    IdempotencyRecord,
    ParentDeposit,
    RefundStatus,
    Subscription,
    SubscriptionPlan,
    SubscriptionSlot,
    SubscriptionStatus,
    Transaction,
    TransactionStatus,
)
from apps.billing.ports import SchedulePort, UnknownSlotError
from apps.billing.selectors import active_seat_q, attendances_awaiting_debit
from apps.core.locks import advisory_xact_lock, advisory_xact_lock_many
from apps.core.queue import kiq_safely
from apps.users.models import Student

logger = logging.getLogger(__name__)


class BillingError(Exception):
    pass


class AttendanceNotFoundError(BillingError):
    def __init__(self, attendance_id: int) -> None:
        super().__init__(f"Отметка посещения id={attendance_id} не найдена.")
        self.attendance_id = attendance_id


class AttendanceNotDebitableError(BillingError):
    def __init__(self, attendance_id: int, status: str) -> None:
        super().__init__(
            f"Отметка id={attendance_id} со статусом {status} не подлежит списанию фишки."
        )
        self.attendance_id = attendance_id
        self.status = status


class SlotBalanceNotFoundError(BillingError):
    def __init__(self, subscription_id: int, slot_id: int) -> None:
        super().__init__(
            f"Баланс фишек не найден: subscription={subscription_id}, slot={slot_id}."
        )
        self.subscription_id = subscription_id
        self.slot_id = slot_id


class InsufficientTokensError(BillingError):
    def __init__(self, subscription_slot_id: int) -> None:
        super().__init__(
            f"Фишки исчерпаны: subscription_slot id={subscription_slot_id}."
        )
        self.subscription_slot_id = subscription_slot_id


class SubscriptionNotSpendableError(BillingError):
    def __init__(self, subscription_id: int, status: str) -> None:
        super().__init__(
            f"Абонемент id={subscription_id} (status={status}) не допускает списание."
        )
        self.subscription_id = subscription_id
        self.status = status


class PlanNotFoundError(BillingError):
    def __init__(self, plan_id: int) -> None:
        super().__init__(f"Тарифный план id={plan_id} не найден.")
        self.plan_id = plan_id


class PlanSlotsMismatchError(BillingError):
    # ПОЧЕМУ: защита от подмены цены со стороны клиента,
    # число переданных слотов обязано строго совпадать с тарифом

    def __init__(self, plan_id: int, expected: int, actual: int) -> None:
        super().__init__(
            f"Тариф id={plan_id} рассчитан на {expected} слот(ов), передано {actual}."
        )
        self.plan_id = plan_id
        self.expected = expected
        self.actual = actual


class IdempotencyKeyReusedError(BillingError):
    def __init__(self, key: str) -> None:
        super().__init__(f"Idempotency-Key {key} уже использован с другим запросом.")
        self.key = key


class PaymentInProgressError(BillingError):
    def __init__(self, key: str) -> None:
        super().__init__(f"Платёж по ключу {key} уже обрабатывается.")
        self.key = key


class PaymentGatewayUnavailableError(BillingError):
    # ПОЧЕМУ: заказ уже аннулирован и резервация ключа снята — клиент может
    # безопасно повторить запрос с тем же Idempotency-Key
    def __init__(self, key: str) -> None:
        super().__init__(f"Шлюз не создал платёж по ключу {key}; заказ аннулирован.")
        self.key = key


class _IdempotencyLockLostError(Exception):
    # ПОЧЕМУ: это control-flow исключение внутри create_payment,
    # оно не является бизнес-ошибкой и наружу не пробрасывается

    def __init__(self, key: str) -> None:
        super().__init__(f"Резервация ключа {key} потеряна во время обработки.")
        self.key = key


class CorruptedIdempotencyRecordError(BillingError):
    def __init__(self, key: str) -> None:
        super().__init__(f"Повреждённая запись идемпотентности key={key}.")
        self.key = key


class UnlinkedPaymentError(BillingError):
    def __init__(self, payment_id: str, reason: str) -> None:
        super().__init__(f"Платёж {payment_id}: {reason}")
        self.payment_id = payment_id
        self.reason = reason


class AmountMismatchError(BillingError):
    def __init__(
        self,
        payment_id: str,
        expected_kopecks: int,
        actual_kopecks: int,
        actual_currency: str,
    ) -> None:
        super().__init__(
            f"Платёж {payment_id}: ожидалось {expected_kopecks} коп. RUB, "
            f"получено {actual_kopecks} коп. {actual_currency}."
        )
        self.payment_id = payment_id
        self.expected_kopecks = expected_kopecks
        self.actual_kopecks = actual_kopecks
        self.actual_currency = actual_currency


class RefundNotAwaitingManualError(BillingError):
    def __init__(self, transaction_id: uuid.UUID) -> None:
        super().__init__(
            f"Транзакция {transaction_id} не ждёт ручного разбора возврата."
        )
        self.transaction_id = transaction_id


class SubscriptionNotActivatableError(BillingError):
    def __init__(self, payment_id: str, subscription_id: int) -> None:
        super().__init__(
            f"Платёж {payment_id}: абонемент {subscription_id} уже не активируем, "
            "требуется компенсация (возврат)."
        )
        self.payment_id = payment_id
        self.subscription_id = subscription_id


class PaymentSucceededAfterExpiryError(BillingError):
    def __init__(self, payment_id: str, transaction_id: str) -> None:
        super().__init__(
            f"Платёж {payment_id}: успех пришёл после истечения TTL транзакции "
            f"{transaction_id}; требуется возврат."
        )
        self.payment_id = payment_id
        self.transaction_id = transaction_id


class StudentNotOwnedError(BillingError):
    def __init__(self, student_id: int) -> None:
        super().__init__(f"Ребёнок id={student_id} не принадлежит этому родителю.")
        self.student_id = student_id


class SlotNotFoundError(BillingError):
    def __init__(self, slot_id: int) -> None:
        super().__init__(f"Слот id={slot_id} не найден в расписании.")
        self.slot_id = slot_id


class NoAvailableSeatsError(BillingError):
    def __init__(self, slot_id: int) -> None:
        super().__init__(f"В слоте id={slot_id} не осталось свободных мест.")
        self.slot_id = slot_id


class DuplicateEnrollmentError(BillingError):
    def __init__(self, student_id: int, slot_id: int) -> None:
        super().__init__(
            f"Ребёнок id={student_id} уже записан/забронирован в слот id={slot_id}."
        )
        self.student_id = student_id
        self.slot_id = slot_id


class SeatsTakenAfterPaymentError(BillingError):
    def __init__(self, payment_id: str, subscription_id: int, reason: str) -> None:
        super().__init__(
            f"Платёж {payment_id}: заказ по абонементу {subscription_id} "
            f"не исполним ({reason}); требуется возврат."
        )
        self.payment_id = payment_id
        self.subscription_id = subscription_id
        self.reason = reason


class EnrollmentNotEnrolledError(BillingError):
    def __init__(self, enrollment_id: int, status: str) -> None:
        super().__init__(
            f"Запись id={enrollment_id} в статусе {status} не допускает списание."
        )
        self.enrollment_id = enrollment_id
        self.status = status


class TrialLimitExceededError(BillingError):
    # ПОЧЕМУ: инвариант №5 — максимум 1 пробное на ребёнка по кружку
    def __init__(self, student_id: int, activity_id: int) -> None:
        super().__init__(
            f"У ребёнка id={student_id} уже есть пробное по кружку id={activity_id}."
        )
        self.student_id = student_id
        self.activity_id = activity_id


class TrialDateUnavailableError(BillingError):
    def __init__(self, slot_id: int, trial_date: date, reason: str) -> None:
        super().__init__(f"Слот id={slot_id}, дата {trial_date.isoformat()}: {reason}")
        self.slot_id = slot_id
        self.trial_date = trial_date
        self.reason = reason


CheckoutStatus = Literal["PENDING_PAYMENT", "CONFIRMED"]


@dataclass(frozen=True)
class CheckoutResult:
    transaction_id: uuid.UUID
    status: CheckoutStatus
    payment_url: str | None
    expires_at: datetime | None


_EXPECTED_CURRENCY = "RUB"
_IN_PROGRESS_STATUS = 202
_SUCCESS_STATUS = 201
_RESERVATION_TTL = timedelta(minutes=15)
_PENDING_TRANSACTION_TTL = timedelta(minutes=15)
_TTL_EXPIRED_REASON = "TTL_EXPIRED"
_CHECKOUT_ABORTED_REASON = "CHECKOUT_ABORTED"
# ПОЧЕМУ: если по аннулированному заказу всё же придёт успешный платёж
# (клиент оплатил по устаревшей ссылке), обе причины ведут в возврат
_REFUNDABLE_CANCEL_REASONS = frozenset({_TTL_EXPIRED_REASON, _CHECKOUT_ABORTED_REASON})
_SLOT_LOCK_CLASS = 815_001

# ПОЧЕМУ: лимит выборки защищает воркер от OOM Death Loop
# при массовой отмене или падении БД
_SWEEP_CHUNK_SIZE = 1_000
_REFUND_CHUNK_SIZE = 500
# ПОЧЕМУ: каждая сверка — GET в ЮКассу с таймаутом до 5 с. 50 × 5 с укладываются
# в интервал крона (5 мин), тогда как весь чанк в 1000 занял бы больше часа
_RECONCILE_CHUNK_SIZE = 50
# ПОЧЕМУ: если ЮКасса недоступна дольше этого срока, заказ снимается без сверки —
# иначе сломанные ключи API держали бы места в группах бесконечно. Оплату
# поймают опоздавший вебхук или досверка и отправят в возврат
_RECONCILE_GIVE_UP_AFTER = timedelta(hours=2)
# ПОЧЕМУ: неоплаченный платёж ЮКасса отменяет сама (expired_on_confirmation), срок
# зависит от способа оплаты. Сутки с запасом накрывают его; не получили
# окончательного статуса — алерт и ручная проверка в кабинете ЮКассы
_POST_EXPIRY_RECHECK_WINDOW = timedelta(hours=24)
# ПОЧЕМУ: lease обязан переживать сетевой вызов к шлюзу с ретраями,
# но не блокировать возврат надолго после смерти воркера
_REFUND_CLAIM_TTL = timedelta(minutes=10)
# ПОЧЕМУ: CANCELED — ответ ЮКассы «возврат не прошёл»; вместе с нашим
# карантином это всё, что требует рук менеджера
REFUND_STATUSES_AWAITING_MANUAL = frozenset(
    {RefundStatus.FAILED, RefundStatus.CANCELED}
)


# ПОЧЕМУ: дефолтное значение для снапшота, бизнес-правила могут меняться,
# поэтому токены жестко фиксируются в БД на момент покупки
# ПОЧЕМУ: абонемент действует месяц от первого занятия, но точное окно
# известно только при подтверждении оплаты. Пять недель гарантированно
# накрывают этот месяц, куда бы ни попало первое занятие
_SUBSCRIPTION_SEAT_HORIZON = timedelta(weeks=5)

_TOKENS_PER_SLOT = 4

# ПОЧЕМУ: Контракт §2.1
_IDEMPOTENCY_RECORD_TTL = timedelta(hours=24)


def debit_token(attendance_id: int) -> None:
    # !!!: операция обязана оставаться идемпотентной и защищенной
    # от гонок (race conditions) на уровне транзакций БД
    with db_transaction.atomic():
        try:
            attendance = (
                Attendance.objects.select_for_update(of=("self",))
                .select_related("enrollment__subscription")
                .get(pk=attendance_id)
            )
        except Attendance.DoesNotExist as exc:
            raise AttendanceNotFoundError(attendance_id) from exc

        if attendance.token_debited:
            return

        if attendance.status != AttendanceStatus.ATTENDED:
            raise AttendanceNotDebitableError(attendance_id, attendance.status)

        enrollment = attendance.enrollment
        if enrollment.status != EnrollmentStatus.ENROLLED:
            raise EnrollmentNotEnrolledError(enrollment.pk, enrollment.status)

        # ПОЧЕМУ: пробное оплачено разовой транзакцией, фишек у него нет —
        # отметка посещения фиксируется без движения баланса
        if enrollment.subscription_id is None:
            return

        subscription = enrollment.subscription
        assert subscription is not None  # сужение для mypy: проверено выше
        now = timezone.now()
        is_expired = (
            subscription.expires_at is not None and subscription.expires_at < now
        )
        if subscription.status != SubscriptionStatus.ACTIVE or is_expired:
            raise SubscriptionNotSpendableError(subscription.pk, subscription.status)

        _spend_slot_token(attendance, subscription.pk)


def _spend_slot_token(attendance: Attendance, subscription_id: int) -> None:
    # ПОЧЕМУ: общий хвост debit_token и добора в свипере истечения. Вызывающий
    # уже держит FOR UPDATE на отметке и проверил, что списывать можно;
    # исключения бросаются до записи — откатывать нечего
    try:
        slot = SubscriptionSlot.objects.select_for_update(of=("self",)).get(
            subscription_id=subscription_id,
            slot_id=attendance.enrollment.schedule_id,
        )
    except SubscriptionSlot.DoesNotExist as exc:
        raise SlotBalanceNotFoundError(
            subscription_id, attendance.enrollment.schedule_id
        ) from exc

    if slot.remaining_tokens <= 0:
        raise InsufficientTokensError(slot.pk)

    slot.remaining_tokens -= 1
    slot.save(update_fields=["remaining_tokens"])
    attendance.token_debited = True
    attendance.save(update_fields=["token_debited"])


def create_payment(
    parent_id: int,
    plan_id: int,
    student_id: int,
    slot_ids: Sequence[int],
    idempotency_key: str,
    request_fingerprint: str,
    *,
    gateway: PaymentGateway,
    schedule_port: SchedulePort,
    use_deposit: bool = False,
) -> CheckoutResult:
    # !!!: строгий порядок блокировок (сначала слоты, затем депозит)
    # исключает взаимную блокировку (ABBA-дедлок) при конкурентных запросах
    try:
        plan = SubscriptionPlan.objects.get(pk=plan_id)
    except SubscriptionPlan.DoesNotExist as exc:
        raise PlanNotFoundError(plan_id) from exc

    normalized_slot_ids = sorted({int(slot_id) for slot_id in slot_ids})
    if len(normalized_slot_ids) != plan.slots_count:
        raise PlanSlotsMismatchError(
            plan_id, plan.slots_count, len(normalized_slot_ids)
        )

    # ПОЧЕМУ: защита от IDOR, проверяем принадлежность ребенка плательщику.
    # Быстрый отказ до резервации ключа; под локом проверка повторяется
    _ensure_student_owned(student_id, parent_id)

    replay, lock_token = _reserve_idempotency(idempotency_key, request_fingerprint)
    if replay is not None:
        return replay

    try:
        with db_transaction.atomic():
            _lock_owned_student(student_id, parent_id)
            subscription = Subscription.objects.create(
                parent_id=parent_id,
                plan=plan,
                status=SubscriptionStatus.PENDING,
                purchase_price=plan.price,
                base_session_price=plan.base_session_price,
            )

            # !!!: локи берутся на весь набор ДО любых проверок и строго в
            # порядке возрастания slot_id (normalized_slot_ids отсортирован
            # выше). Разный порядок захвата в параллельных корзинах с
            # пересекающимися слотами — гарантированный дедлок на advisory-локах
            _lock_slots_for_booking(normalized_slot_ids)

            # ПОЧЕМУ: partial-индекс БД не вычисляет динамический now(),
            # чистим протухшие HELD вручную для разблокировки ретрая клиента.
            # Один UPDATE на весь набор вместо N
            hold_expired_before = timezone.now() - _PENDING_TRANSACTION_TTL
            Enrollment.objects.filter(
                schedule_id__in=normalized_slot_ids,
                status=EnrollmentStatus.HELD,
                created_at__lt=hold_expired_before,
            ).update(status=EnrollmentStatus.CANCELED)

            capacities: dict[int, int] = {}
            for slot_id in normalized_slot_ids:
                try:
                    capacities[slot_id] = schedule_port.get_slot_capacity(slot_id)
                except UnknownSlotError as exc:
                    raise SlotNotFoundError(slot_id) from exc

            # ПОЧЕМУ батч: занятость всех слотов набора считается двумя
            # запросами вместо 2N — локи не простаивают на пачке round-trip'ов
            occupied = _occupied_seats_bulk(normalized_slot_ids)
            for slot_id in normalized_slot_ids:
                if occupied.get(slot_id, 0) >= capacities[slot_id]:
                    raise NoAvailableSeatsError(slot_id)

            for slot_id in normalized_slot_ids:
                try:
                    # ПОЧЕМУ не bulk_create: INSERT — неизбежная работа, а
                    # поштучный вызов сохраняет точную атрибуцию конфликта
                    Enrollment.objects.create(
                        student_id=student_id,
                        subscription=subscription,
                        schedule_id=slot_id,
                        status=EnrollmentStatus.HELD,
                    )
                except IntegrityError as exc:
                    raise DuplicateEnrollmentError(student_id, slot_id) from exc

            deposit_applied = 0
            deposit: ParentDeposit | None = None
            if use_deposit:
                deposit = (
                    ParentDeposit.objects.select_for_update()
                    .filter(parent_id=parent_id)
                    .first()
                )
                if deposit is not None and deposit.balance > 0:
                    deposit_applied = min(deposit.balance, plan.price)
                    deposit.balance -= deposit_applied
                    deposit.save(update_fields=["balance", "updated_at"])

            tx_metadata: dict[str, object] = {}
            if deposit_applied > 0:
                tx_metadata["deposit_applied_kopecks"] = deposit_applied

            tx = Transaction.objects.create(
                parent_id=parent_id,
                subscription=subscription,
                amount=plan.price - deposit_applied,
                status=TransactionStatus.PENDING,
                selected_slot_ids=normalized_slot_ids,
                metadata=tx_metadata,
            )
            if deposit_applied > 0 and deposit is not None:
                DepositEntry.objects.create(
                    deposit=deposit,
                    amount=-deposit_applied,
                    reason=DepositEntryReason.CHECKOUT_SPEND,
                    transaction=tx,
                )

            if tx.amount == 0:
                _fulfill_prepaid_order(tx, schedule_port)
                result = CheckoutResult(
                    transaction_id=tx.pk,
                    status="CONFIRMED",
                    payment_url=None,
                    expires_at=None,
                )
                _finalize_idempotency_record(idempotency_key, lock_token, result)
                return result
    except _IdempotencyLockLostError:
        return _recover_lost_idempotency(idempotency_key)
    except Exception:
        _release_reservation(idempotency_key, lock_token)
        raise

    # ПОЧЕМУ: сетевой вызов к шлюзу вынесен за границы транзакции — advisory-локи
    # слотов и коннект из пула БД не удерживаются на время HTTP (контракт
    # apps.billing.ports запрещает I/O под транзакцией)
    try:
        payment = gateway.create_payment(
            amount_kopecks=tx.amount,
            transaction_id=str(tx.pk),
            idempotence_key=f"payment-{tx.pk}",
            description=f"Абонемент «{plan.name}»",
        )
    except GatewayError as exc:
        # Заказ уже закоммичен — аннулируем компенсацией, а не откатом
        logger.warning(
            "Checkout %s: шлюз не создал платёж (%s); заказ аннулирован.",
            tx.pk,
            exc,
        )
        _void_unpaid_order(tx.pk)
        _release_reservation(idempotency_key, lock_token)
        raise PaymentGatewayUnavailableError(idempotency_key) from exc

    result = CheckoutResult(
        transaction_id=tx.pk,
        status="PENDING_PAYMENT",
        payment_url=payment.confirmation_url,
        expires_at=tx.created_at + _PENDING_TRANSACTION_TTL,
    )
    try:
        with db_transaction.atomic():
            Transaction.objects.filter(
                pk=tx.pk, status=TransactionStatus.PENDING
            ).update(external_id=payment.id)
            _finalize_idempotency_record(idempotency_key, lock_token, result)
    except _IdempotencyLockLostError:
        # Гонка проиграна конкуренту, перехватившему протухший лок: наш заказ
        # обязан быть аннулирован, клиенту возвращается результат победителя
        _void_unpaid_order(tx.pk)
        return _recover_lost_idempotency(idempotency_key)
    except Exception:
        _void_unpaid_order(tx.pk)
        _release_reservation(idempotency_key, lock_token)
        raise

    return result


def _reserve_idempotency(
    key: str, fingerprint: str
) -> tuple[CheckoutResult | None, uuid.UUID]:
    # ПОЧЕМУ: общий пролог всех чекаутов — get_or_create резервации,
    # replay финализированного ответа, перехват протухшего лока (fencing)
    now = timezone.now()
    lock_token = uuid.uuid4()
    record, created = IdempotencyRecord.objects.get_or_create(
        key=key,
        defaults={
            "request_fingerprint": fingerprint,
            "response_status": _IN_PROGRESS_STATUS,
            "response_body": {},
            "locked_until": now + _RESERVATION_TTL,
            "lock_token": lock_token,
        },
    )
    if not created:
        if record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReusedError(key)
        if record.response_status != _IN_PROGRESS_STATUS:
            return _result_from_record(record), lock_token
        reclaimed = IdempotencyRecord.objects.filter(
            key=key,
            response_status=_IN_PROGRESS_STATUS,
            locked_until__lt=now,
        ).update(locked_until=now + _RESERVATION_TTL, lock_token=lock_token)
        if reclaimed == 0:
            raise PaymentInProgressError(key)
    return None, lock_token


def _finalize_idempotency_record(
    key: str, lock_token: uuid.UUID, result: CheckoutResult
) -> None:
    finalized = IdempotencyRecord.objects.filter(
        key=key,
        response_status=_IN_PROGRESS_STATUS,
        lock_token=lock_token,
    ).update(
        response_status=_SUCCESS_STATUS,
        response_body={
            "payment_url": result.payment_url,
            "transaction_id": str(result.transaction_id),
            "status": result.status,
            "expires_at": (
                result.expires_at.isoformat() if result.expires_at is not None else None
            ),
        },
        locked_until=None,
        lock_token=None,
    )
    if finalized == 0:
        raise _IdempotencyLockLostError(key)


def _recover_lost_idempotency(key: str) -> CheckoutResult:
    current = IdempotencyRecord.objects.filter(key=key).first()
    if current is not None and current.response_status == _SUCCESS_STATUS:
        return _result_from_record(current)
    raise PaymentInProgressError(key) from None


def _void_unpaid_order(transaction_id: uuid.UUID) -> None:
    # ПОЧЕМУ: причина из _REFUNDABLE_CANCEL_REASONS — если провайдер всё же
    # проведёт оплату по аннулированному заказу, _apply_success отправит возврат
    with db_transaction.atomic():
        tx = (
            Transaction.objects.select_for_update()
            .filter(pk=transaction_id, status=TransactionStatus.PENDING)
            .first()
        )
        if tx is None:
            return
        tx.status = TransactionStatus.CANCELED
        tx.metadata = {**tx.metadata, "canceled_reason": _CHECKOUT_ABORTED_REASON}
        tx.save(update_fields=["status", "metadata"])
        _release_order_resources(tx)


def create_trial_payment(
    parent_id: int,
    student_id: int,
    schedule_id: int,
    trial_date: date,
    idempotency_key: str,
    request_fingerprint: str,
    *,
    gateway: PaymentGateway,
    schedule_port: SchedulePort,
) -> CheckoutResult:
    # ПОЧЕМУ: защита от IDOR — принадлежность ребёнка плательщику
    _ensure_student_owned(student_id, parent_id)
    now = timezone.now()
    if trial_date < timezone.localdate(now):
        raise TrialDateUnavailableError(
            schedule_id, trial_date, "дата пробного уже в прошлом."
        )

    replay, lock_token = _reserve_idempotency(idempotency_key, request_fingerprint)
    if replay is not None:
        return replay

    try:
        with db_transaction.atomic():
            _lock_owned_student(student_id, parent_id)
            _lock_slot_for_booking(schedule_id)
            hold_expired_before = timezone.now() - _PENDING_TRANSACTION_TTL
            Enrollment.objects.filter(
                schedule_id=schedule_id,
                status=EnrollmentStatus.HELD,
                created_at__lt=hold_expired_before,
            ).update(status=EnrollmentStatus.CANCELED)

            try:
                trial_info = schedule_port.get_slot_trial_info(schedule_id)
                capacity = schedule_port.get_slot_capacity(schedule_id)
                next_lesson = schedule_port.get_next_lesson_date(
                    schedule_id,
                    max(
                        now, timezone.make_aware(datetime.combine(trial_date, time.min))
                    ),
                )
            except UnknownSlotError as exc:
                raise SlotNotFoundError(schedule_id) from exc
            # ПОЧЕМУ: дата валидна, только если ближайшее занятие с открытой
            # записью — ровно эта дата; отмены, переносы и уже начавшееся
            # сегодняшнее занятие учтены портом
            if next_lesson != trial_date:
                raise TrialDateUnavailableError(
                    schedule_id,
                    trial_date,
                    "на эту дату нет занятия, на которое ещё открыта запись.",
                )

            # Быстрая проверка лимита до capacity-работы; гонку двух параллельных
            # покупок на разных слотах одного кружка ловит partial-unique в БД
            if Enrollment.objects.filter(
                student_id=student_id,
                activity_id=trial_info.activity_id,
                type=EnrollmentType.TRIAL,
                status__in=(EnrollmentStatus.HELD, EnrollmentStatus.ENROLLED),
            ).exists():
                raise TrialLimitExceededError(student_id, trial_info.activity_id)

            # ПОЧЕМУ не в БД: уникальность в группе держится только для REGULAR,
            # пробное поверх абонемента запрещаем здесь. Гонку с чекаутом
            # абонемента исключает тот же advisory-лок слота
            if Enrollment.objects.filter(
                student_id=student_id,
                schedule_id=schedule_id,
                type=EnrollmentType.REGULAR,
                status__in=(EnrollmentStatus.HELD, EnrollmentStatus.ENROLLED),
            ).exists():
                raise DuplicateEnrollmentError(student_id, schedule_id)

            if _occupied_seats(schedule_id, on_date=trial_date) >= capacity:
                raise NoAvailableSeatsError(schedule_id)

            try:
                # ПОЧЕМУ вложенный atomic: savepoint позволяет классифицировать
                # IntegrityError запросами — без него транзакция уже abort-нута
                with db_transaction.atomic():
                    enrollment = Enrollment.objects.create(
                        student_id=student_id,
                        subscription=None,
                        schedule_id=schedule_id,
                        activity_id=trial_info.activity_id,
                        type=EnrollmentType.TRIAL,
                        trial_date=trial_date,
                        status=EnrollmentStatus.HELD,
                    )
            except IntegrityError as exc:
                if Enrollment.objects.filter(
                    student_id=student_id,
                    activity_id=trial_info.activity_id,
                    type=EnrollmentType.TRIAL,
                    status__in=(EnrollmentStatus.HELD, EnrollmentStatus.ENROLLED),
                ).exists():
                    raise TrialLimitExceededError(
                        student_id, trial_info.activity_id
                    ) from exc
                raise DuplicateEnrollmentError(student_id, schedule_id) from exc

            tx = Transaction.objects.create(
                parent_id=parent_id,
                enrollment=enrollment,
                amount=trial_info.price_kopecks,
                status=TransactionStatus.PENDING,
                selected_slot_ids=[schedule_id],
                metadata={"kind": "trial", "trial_date": trial_date.isoformat()},
            )

            if tx.amount == 0:
                # ПОЧЕМУ: бесплатное пробное подтверждается без похода в кассу —
                # симметрично бесплатным ивентам и депозитному абонементу
                tx.status = TransactionStatus.SUCCEEDED
                tx.metadata = {**tx.metadata, "free_trial": True}
                tx.save(update_fields=["status", "metadata"])
                enrollment.status = EnrollmentStatus.ENROLLED
                enrollment.save(update_fields=["status"])
                result = CheckoutResult(
                    transaction_id=tx.pk,
                    status="CONFIRMED",
                    payment_url=None,
                    expires_at=None,
                )
                _finalize_idempotency_record(idempotency_key, lock_token, result)
                return result
    except _IdempotencyLockLostError:
        return _recover_lost_idempotency(idempotency_key)
    except Exception:
        _release_reservation(idempotency_key, lock_token)
        raise

    try:
        payment = gateway.create_payment(
            amount_kopecks=tx.amount,
            transaction_id=str(tx.pk),
            idempotence_key=f"payment-{tx.pk}",
            description=f"Пробное занятие {trial_date.isoformat()}",
        )
    except GatewayError as exc:
        logger.warning(
            "Trial checkout %s: шлюз не создал платёж (%s); заказ аннулирован.",
            tx.pk,
            exc,
        )
        _void_unpaid_order(tx.pk)
        _release_reservation(idempotency_key, lock_token)
        raise PaymentGatewayUnavailableError(idempotency_key) from exc

    result = CheckoutResult(
        transaction_id=tx.pk,
        status="PENDING_PAYMENT",
        payment_url=payment.confirmation_url,
        expires_at=tx.created_at + _PENDING_TRANSACTION_TTL,
    )
    try:
        with db_transaction.atomic():
            Transaction.objects.filter(
                pk=tx.pk, status=TransactionStatus.PENDING
            ).update(external_id=payment.id)
            _finalize_idempotency_record(idempotency_key, lock_token, result)
    except _IdempotencyLockLostError:
        _void_unpaid_order(tx.pk)
        return _recover_lost_idempotency(idempotency_key)
    except Exception:
        _void_unpaid_order(tx.pk)
        _release_reservation(idempotency_key, lock_token)
        raise

    return result


def _fulfill_prepaid_order(tx: Transaction, schedule_port: SchedulePort) -> None:
    subscription_id = tx.subscription_id
    if subscription_id is None:
        raise UnlinkedPaymentError(
            "deposit-prepaid", f"транзакция {tx.pk} не связана с абонементом"
        )
    slot_ids = _selected_slot_ids(tx, "deposit-prepaid")
    try:
        first_lessons = _first_lessons(slot_ids, schedule_port)
    except UnknownSlotError as exc:
        raise SlotNotFoundError(exc.slot_id) from exc
    start_date, expires_at = _activation_window(first_lessons)

    tx.status = TransactionStatus.SUCCEEDED
    tx.metadata = {**tx.metadata, "paid_from_deposit": True}
    tx.save(update_fields=["status", "metadata"])
    Subscription.objects.filter(
        pk=subscription_id, status=SubscriptionStatus.PENDING
    ).update(
        status=SubscriptionStatus.ACTIVE,
        start_date=start_date,
        expires_at=expires_at,
    )
    Enrollment.objects.filter(
        subscription_id=subscription_id, status=EnrollmentStatus.HELD
    ).update(status=EnrollmentStatus.ENROLLED)
    _join_todays_lessons(subscription_id, first_lessons)
    SubscriptionSlot.objects.bulk_create(
        [
            SubscriptionSlot(
                subscription_id=subscription_id,
                slot_id=slot_id,
                granted_tokens=_TOKENS_PER_SLOT,
                remaining_tokens=_TOKENS_PER_SLOT,
            )
            for slot_id in slot_ids
        ]
    )


def _ensure_student_owned(student_id: int, parent_id: int) -> None:
    # Удалённый родителем (архивный) ребёнок для чекаута — как чужой
    owned = Student.objects.active().filter(pk=student_id, parent_id=parent_id)
    if not owned.exists():
        raise StudentNotOwnedError(student_id)


def _lock_owned_student(student_id: int, parent_id: int) -> None:
    # !!!: первый лок транзакции чекаута (ребёнок -> слоты -> депозит).
    # FOR NO KEY UPDATE конфликтует с таким же локом в archive_child: иначе
    # родитель мог удалить ребёнка между проверкой владения и созданием брони.
    # Неявный FOR KEY SHARE от FK при INSERT брони с UPDATE не конфликтует
    locked = (
        Student.objects.active()
        .select_for_update(no_key=True)
        .filter(pk=student_id, parent_id=parent_id)
        .values_list("pk", flat=True)
        .first()
    )
    if locked is None:
        raise StudentNotOwnedError(student_id)


def _lock_slot_for_booking(slot_id: int) -> None:
    # ПОЧЕМУ: в биллинге нет строки слота для FOR UPDATE (слот — чужой домен)
    advisory_xact_lock(_SLOT_LOCK_CLASS, slot_id)


def _lock_slots_for_booking(slot_ids: Sequence[int]) -> None:
    # !!!: slot_ids обязаны быть отсортированы — порядок захвата определяет
    # отсутствие дедлока между пересекающимися корзинами
    advisory_xact_lock_many(_SLOT_LOCK_CLASS, slot_ids)


def _occupied_seats_bulk(
    slot_ids: Sequence[int],
    *,
    on_date: date | None = None,
    exclude_enrollment_pks: Sequence[int] | None = None,
) -> dict[int, int]:
    # !!!: занятость места имеет ось времени. Постоянная запись держит место на
    # КАЖДОМ занятии слота, пробное — ровно на одной дате. Складывать их плоским
    # count() нельзя: десять пробных на десять разных недель «съели» бы всю
    # группу навсегда, хотя физически в каждый день занят один стул.
    #
    # on_date задана (покупка/подтверждение пробного) — занятость строго этого
    # дня. on_date не задана (абонемент, покрывающий ~месяц занятий) — пик:
    # постоянные плюс самый загруженный пробными день горизонта, потому что
    # абонемент обязан иметь место на каждом занятии, а не в среднем.
    #
    # ПОЧЕМУ батч: вызывается под захваченными advisory-локами. Поштучный
    # подсчёт держал бы локи на 2N сетевых round-trip'ах вместо двух
    if not slot_ids:
        return {}

    base = Enrollment.objects.filter(schedule_id__in=slot_ids).filter(active_seat_q())
    if exclude_enrollment_pks:
        base = base.exclude(pk__in=exclude_enrollment_pks)

    occupied: dict[int, int] = dict(
        base.filter(type=EnrollmentType.REGULAR)
        .values("schedule_id")
        .annotate(taken=Count("id"))
        .values_list("schedule_id", "taken")
    )

    trials = base.filter(type=EnrollmentType.TRIAL)
    if on_date is not None:
        for slot_id, taken in (
            trials.filter(trial_date=on_date)
            .values("schedule_id")
            .annotate(taken=Count("id"))
            .values_list("schedule_id", "taken")
        ):
            occupied[slot_id] = occupied.get(slot_id, 0) + taken
        return occupied

    today = timezone.localdate()
    peaks: dict[int, int] = {}
    for slot_id, _trial_date, taken in (
        trials.filter(
            trial_date__gte=today,
            trial_date__lt=today + _SUBSCRIPTION_SEAT_HORIZON,
        )
        .values("schedule_id", "trial_date")
        .annotate(taken=Count("id"))
        .values_list("schedule_id", "trial_date", "taken")
    ):
        peaks[slot_id] = max(peaks.get(slot_id, 0), taken)
    for slot_id, peak in peaks.items():
        occupied[slot_id] = occupied.get(slot_id, 0) + peak
    return occupied


def _occupied_seats(
    slot_id: int,
    *,
    on_date: date | None = None,
    exclude_enrollment_pk: int | None = None,
) -> int:
    excluded = [exclude_enrollment_pk] if exclude_enrollment_pk is not None else None
    return _occupied_seats_bulk(
        [slot_id], on_date=on_date, exclude_enrollment_pks=excluded
    ).get(slot_id, 0)


def _release_reservation(idempotency_key: str, lock_token: uuid.UUID) -> int:
    return IdempotencyRecord.objects.filter(
        key=idempotency_key,
        response_status=_IN_PROGRESS_STATUS,
        lock_token=lock_token,
    ).delete()[0]


def confirm_payment(
    *, payment_id: str, gateway: PaymentGateway, schedule_port: SchedulePort
) -> None:
    # !!!: мы не доверяем payload вебхука
    # применяем строго верифицированный статус напрямую из API провайдера
    info = gateway.get_payment(payment_id)
    _apply_verified_payment(info, _verified_transaction_id(info), schedule_port)


def _apply_verified_payment(
    info: PaymentInfo, transaction_id: uuid.UUID, schedule_port: SchedulePort
) -> None:
    # ПОЧЕМУ: общий хвост вебхука и сверки в свипере — статус уже получен
    # из API провайдера, обе дороги проводят его одним и тем же кодом
    if info.status == "succeeded":
        _apply_success(info, transaction_id, schedule_port)
        return
    if info.status == "canceled":
        _apply_cancellation(transaction_id, info.id)
        return
    # ПОЧЕМУ: промежуточные статусы (pending, waiting_for_capture) игнорируются,
    # ожидаем терминального состояния платежа от провайдера


def _verified_transaction_id(info: PaymentInfo) -> uuid.UUID:
    if info.transaction_id is None:
        raise UnlinkedPaymentError(info.id, "в metadata нет transaction_id")
    try:
        return uuid.UUID(info.transaction_id)
    except ValueError as exc:
        raise UnlinkedPaymentError(info.id, "transaction_id не является UUID") from exc


def _apply_success(
    info: PaymentInfo, transaction_id: uuid.UUID, schedule_port: SchedulePort
) -> None:
    deferred: BillingError | None = None

    with db_transaction.atomic():
        try:
            tx = Transaction.objects.select_for_update().get(pk=transaction_id)
        except Transaction.DoesNotExist:
            # ПОЧЕМУ: чужая транзакция или рассинхрон баз, игнорируем
            return

        if tx.status != TransactionStatus.PENDING:
            if (
                tx.status == TransactionStatus.CANCELED
                and tx.metadata.get("canceled_reason") in _REFUNDABLE_CANCEL_REASONS
                # ПОЧЕМУ received_amount, а не флаг возврата: флаг сбрасывается
                # после выплаты, и повторный вебхук поставил бы возврат снова
                and tx.received_amount is None
            ):
                # ПОЧЕМУ: вебхук опоздал, заказ уже аннулирован (TTL-свипер или
                # аварийное прерывание checkout) — инициируем возврат
                tx.external_id = info.id
                tx.received_amount = info.amount_kopecks
                tx.save(update_fields=["external_id", "received_amount"])
                _queue_refund(
                    tx, info.currency, {"reason": "PAYMENT_SUCCEEDED_AFTER_EXPIRY"}
                )
                deferred = PaymentSucceededAfterExpiryError(info.id, str(tx.pk))
        elif info.currency != _EXPECTED_CURRENCY or info.amount_kopecks != tx.amount:
            tx.status = TransactionStatus.FAILED
            tx.external_id = info.id
            tx.received_amount = info.amount_kopecks
            tx.metadata = {
                **tx.metadata,
                "failure_reason": "AMOUNT_MISMATCH",
                "expected_amount_kopecks": tx.amount,
                "expected_currency": _EXPECTED_CURRENCY,
                "gateway_amount_kopecks": info.amount_kopecks,
                "gateway_currency": info.currency,
            }
            tx.save(
                update_fields=["status", "external_id", "received_amount", "metadata"]
            )
            # ПОЧЕМУ: заказ не исполняется, а деньги у нас — возвращаем всё
            # пришедшее; депозитную часть вернёт _release_order_resources
            _queue_refund(tx, info.currency, {})
            _release_order_resources(tx)
            deferred = AmountMismatchError(
                info.id, tx.amount, info.amount_kopecks, info.currency
            )
        elif tx.enrollment_id is not None:
            deferred = _apply_trial_success(info, tx, tx.enrollment_id, schedule_port)
        else:
            data_error = _validate_success_payload(tx, info.id)
            if data_error is not None:
                tx.status = TransactionStatus.FAILED
                tx.external_id = info.id
                tx.received_amount = info.amount_kopecks
                tx.metadata = {
                    **tx.metadata,
                    "failure_reason": "DATA_INTEGRITY",
                    "detail": str(data_error),
                }
                tx.save(
                    update_fields=[
                        "status",
                        "external_id",
                        "received_amount",
                        "metadata",
                    ]
                )
                _mark_for_compensation(tx, {})
                _release_order_resources(tx)
                deferred = data_error
            else:
                slot_ids = _selected_slot_ids(tx, info.id)
                # Сужение Optional: не-None гарантирован _validate_success_payload
                subscription_id = tx.subscription_id
                assert subscription_id is not None
                tx.status = TransactionStatus.SUCCEEDED
                tx.external_id = info.id
                tx.received_amount = info.amount_kopecks
                tx.save(update_fields=["status", "external_id", "received_amount"])

                try:
                    first_lessons = _first_lessons(slot_ids, schedule_port)
                except UnknownSlotError:
                    _mark_for_compensation(
                        tx,
                        {
                            "reason": "SLOT_REMOVED",
                            "subscription_id": tx.subscription_id,
                        },
                    )
                    _release_order_resources(tx)
                    deferred = SeatsTakenAfterPaymentError(
                        info.id, tx.subscription_id or 0, "SLOT_REMOVED"
                    )
                else:
                    start_date, expires_at = _activation_window(first_lessons)
                    activated = Subscription.objects.filter(
                        pk=subscription_id, status=SubscriptionStatus.PENDING
                    ).update(
                        status=SubscriptionStatus.ACTIVE,
                        start_date=start_date,
                        expires_at=expires_at,
                    )
                    if activated == 0:
                        _mark_for_compensation(
                            tx,
                            {
                                "reason": "SUBSCRIPTION_NOT_ACTIVATABLE",
                                "subscription_id": tx.subscription_id,
                            },
                        )
                        _release_order_resources(tx)
                        deferred = SubscriptionNotActivatableError(
                            info.id, tx.subscription_id or 0
                        )
                    else:
                        overbook_reason = _try_enroll_held_seats(
                            subscription_id, slot_ids, schedule_port
                        )
                        if overbook_reason is not None:
                            _mark_for_compensation(
                                tx,
                                {
                                    "reason": overbook_reason,
                                    "subscription_id": subscription_id,
                                },
                            )
                            Subscription.objects.filter(
                                pk=subscription_id,
                                status=SubscriptionStatus.ACTIVE,
                            ).update(
                                status=SubscriptionStatus.CANCELED,
                                start_date=None,
                                expires_at=None,
                            )
                            _release_order_resources(tx)
                            deferred = SeatsTakenAfterPaymentError(
                                info.id, subscription_id, overbook_reason
                            )
                        else:
                            _join_todays_lessons(subscription_id, first_lessons)
                            SubscriptionSlot.objects.bulk_create(
                                [
                                    SubscriptionSlot(
                                        subscription_id=subscription_id,
                                        slot_id=slot_id,
                                        granted_tokens=_TOKENS_PER_SLOT,
                                        remaining_tokens=_TOKENS_PER_SLOT,
                                    )
                                    for slot_id in slot_ids
                                ]
                            )

    if deferred is not None:
        raise deferred


def _apply_trial_success(
    info: PaymentInfo,
    tx: Transaction,
    enrollment_id: int,
    schedule_port: SchedulePort,
) -> BillingError | None:
    # ПОЧЕМУ enrollment_id отдельным аргументом: у tx поле Optional, а
    # вызывающий уже проверил его на None — сужение типа не переживает
    # границу функции, передаём готовый int
    # !!!: вызывается строго под select_for_update по tx из _apply_success.
    # ПОЧЕМУ повторная проверка мест: бронь (HELD) могла протухнуть
    # за время оплаты, а место — уйти конкуренту
    tx.status = TransactionStatus.SUCCEEDED
    tx.external_id = info.id
    tx.received_amount = info.amount_kopecks
    tx.save(update_fields=["status", "external_id", "received_amount"])

    # !!!: порядок захвата (advisory-лок слота → строка Enrollment) обязан
    # совпадать с _try_enroll_held_seats, иначе вебхуки пробного и абонемента
    # по одному слоту ловят взаимный дедлок. schedule_id читаем без лока
    schedule_id: int = Enrollment.objects.values_list("schedule_id", flat=True).get(
        pk=enrollment_id
    )
    _lock_slot_for_booking(schedule_id)
    enrollment = Enrollment.objects.select_for_update().get(pk=enrollment_id)

    if enrollment.status != EnrollmentStatus.HELD:
        _mark_for_compensation(
            tx, {"reason": "HOLD_LOST", "enrollment_id": enrollment.pk}
        )
        _release_order_resources(tx)
        return SeatsTakenAfterPaymentError(info.id, 0, "HOLD_LOST")

    try:
        capacity = schedule_port.get_slot_capacity(enrollment.schedule_id)
    except UnknownSlotError:
        _mark_for_compensation(
            tx, {"reason": "SLOT_REMOVED", "enrollment_id": enrollment.pk}
        )
        _release_order_resources(tx)
        return SeatsTakenAfterPaymentError(info.id, 0, "SLOT_REMOVED")

    taken_by_others = _occupied_seats(
        enrollment.schedule_id,
        on_date=enrollment.trial_date,
        exclude_enrollment_pk=enrollment.pk,
    )
    if taken_by_others >= capacity:
        _mark_for_compensation(
            tx,
            {
                "reason": "SEATS_TAKEN_AFTER_PAYMENT",
                "enrollment_id": enrollment.pk,
            },
        )
        _release_order_resources(tx)
        return SeatsTakenAfterPaymentError(info.id, 0, "SEATS_TAKEN_AFTER_PAYMENT")

    enrollment.status = EnrollmentStatus.ENROLLED
    enrollment.save(update_fields=["status"])
    # ПОЧЕМУ без проверки времени: оплату, пришедшую после начала занятия,
    # принимаем (решение бизнеса 2026-10-01) — ребёнок, скорее всего, уже там
    if enrollment.trial_date == timezone.localdate():
        _add_to_journal({enrollment.pk: enrollment.trial_date})
    return None


def _mark_for_compensation(tx: Transaction, extra: dict[str, object]) -> None:
    tx.requires_compensation = True
    tx.metadata = {**tx.metadata, "compensation_required": True, **extra}
    tx.save(update_fields=["requires_compensation", "metadata"])


def _queue_refund(tx: Transaction, currency: str, extra: dict[str, object]) -> None:
    # ПОЧЕМУ: возврат в ЮКассе делается в валюте платежа, а наш шлюз шлёт только
    # рубли в копейках. Платежи мы создаём строго в RUB, чужая валюта — аномалия:
    # автоматом не возвращаем, отдаём на ручной разбор (экран транзакций в админке)
    if currency == _EXPECTED_CURRENCY:
        _mark_for_compensation(tx, extra)
        return
    tx.refund_status = RefundStatus.FAILED
    tx.metadata = {
        **tx.metadata,
        **extra,
        "refund_error": f"валюта платежа {currency} — возврат вручную",
    }
    tx.save(update_fields=["refund_status", "metadata"])
    logger.error(
        "Транзакция %s: оплата в валюте %s — возврат требует ручного разбора.",
        tx.pk,
        currency,
    )
    _schedule_refund_review_alert(tx.pk)


def _validate_success_payload(
    tx: Transaction, payment_id: str
) -> UnlinkedPaymentError | None:
    if tx.subscription_id is None:
        return UnlinkedPaymentError(
            payment_id, f"транзакция {tx.pk} не связана с абонементом"
        )
    try:
        _selected_slot_ids(tx, payment_id)
    except UnlinkedPaymentError as exc:
        return exc
    return None


def _try_enroll_held_seats(
    subscription_id: int, slot_ids: list[int], schedule_port: SchedulePort
) -> str | None:
    # ПОЧЕМУ: перед финальным зачислением обязательна повторная проверка мест,
    # так как бронь (HELD) могла протухнуть за время проведения платежа
    ordered = sorted(slot_ids)
    _lock_slots_for_booking(ordered)

    # ПОЧЕМУ: multi-row FOR UPDATE без ORDER BY лочит строки в порядке плана;
    # два конкурентных захвата пересекающихся наборов — готовый ABBA-дедлок
    own_by_slot = {
        enrollment.schedule_id: enrollment
        for enrollment in Enrollment.objects.select_for_update()
        .filter(
            subscription_id=subscription_id,
            schedule_id__in=ordered,
            status=EnrollmentStatus.HELD,
        )
        .order_by("pk")
    }

    if any(own_by_slot.get(slot_id) is None for slot_id in ordered):
        return "HOLD_LOST"

    capacities: dict[int, int] = {}
    for slot_id in ordered:
        try:
            capacities[slot_id] = schedule_port.get_slot_capacity(slot_id)
        except UnknownSlotError:
            return "SLOT_REMOVED"

    # ПОЧЕМУ батч: локи на все слоты уже взяты выше, занятость считается
    # двумя запросами на весь набор. Исключаем собственные брони целиком —
    # каждая принадлежит ровно одному слоту набора
    taken = _occupied_seats_bulk(
        ordered,
        exclude_enrollment_pks=[held.pk for held in own_by_slot.values()],
    )
    for slot_id in ordered:
        if taken.get(slot_id, 0) >= capacities[slot_id]:
            return "SEATS_TAKEN_AFTER_PAYMENT"

    for enrollment in own_by_slot.values():
        enrollment.status = EnrollmentStatus.ENROLLED
        enrollment.save(update_fields=["status"])
    return None


def _release_order_resources(tx: Transaction) -> None:
    # !!!: жесткий порядок отката (Enrollment -> Subscription -> депозит)
    # предотвращает ABBA-дедлок с процессом checkout
    if tx.subscription_id is not None:
        Enrollment.objects.filter(
            subscription_id=tx.subscription_id,
            status__in=(EnrollmentStatus.HELD, EnrollmentStatus.ENROLLED),
        ).update(status=EnrollmentStatus.CANCELED)
        Subscription.objects.filter(
            pk=tx.subscription_id, status=SubscriptionStatus.PENDING
        ).update(status=SubscriptionStatus.CANCELED)
    if tx.enrollment_id is not None:
        # Пробное: снятие брони освобождает и лимит «1 пробное на кружок»
        Enrollment.objects.filter(
            pk=tx.enrollment_id,
            status__in=(EnrollmentStatus.HELD, EnrollmentStatus.ENROLLED),
        ).update(status=EnrollmentStatus.CANCELED)
    _return_deposit_hold(tx)


def _return_deposit_hold(tx: Transaction) -> None:
    applied = tx.metadata.get("deposit_applied_kopecks")
    if not isinstance(applied, int) or applied <= 0:
        return
    if tx.metadata.get("deposit_returned"):
        return
    deposit = (
        ParentDeposit.objects.select_for_update().filter(parent_id=tx.parent_id).first()
    )
    if deposit is None:
        return
    try:
        DepositEntry.objects.create(
            deposit=deposit,
            amount=applied,
            reason=DepositEntryReason.ORDER_CANCELED_RETURN,
            transaction=tx,
        )
    except IntegrityError:
        # ПОЧЕМУ: возврат уже проведен параллельным воркером,
        # защита уникальности БД предотвратила задвоение баланса
        return
    deposit.balance += applied
    deposit.save(update_fields=["balance", "updated_at"])
    tx.metadata = {**tx.metadata, "deposit_returned": True}
    tx.save(update_fields=["metadata"])


def _apply_cancellation(transaction_id: uuid.UUID, payment_id: str) -> None:
    with db_transaction.atomic():
        claimed = Transaction.objects.filter(
            pk=transaction_id,
            status=TransactionStatus.PENDING,
        ).update(status=TransactionStatus.CANCELED, external_id=payment_id)
        if claimed == 0:
            return

        tx = Transaction.objects.get(pk=transaction_id)
        _release_order_resources(tx)


def _result_from_record(record: IdempotencyRecord) -> CheckoutResult:
    body = record.response_body
    if not isinstance(body, dict) or "payment_url" not in body:
        raise CorruptedIdempotencyRecordError(record.key)
    url = body["payment_url"]
    if url is not None and not isinstance(url, str):
        raise CorruptedIdempotencyRecordError(record.key)
    raw_transaction_id = body.get("transaction_id")
    raw_status = body.get("status")
    if not isinstance(raw_transaction_id, str) or raw_status not in (
        "PENDING_PAYMENT",
        "CONFIRMED",
    ):
        raise CorruptedIdempotencyRecordError(record.key)
    try:
        transaction_id = uuid.UUID(raw_transaction_id)
    except ValueError as exc:
        raise CorruptedIdempotencyRecordError(record.key) from exc
    raw_expires = body.get("expires_at")
    expires_at: datetime | None = None
    if raw_expires is not None:
        if not isinstance(raw_expires, str):
            raise CorruptedIdempotencyRecordError(record.key)
        try:
            expires_at = datetime.fromisoformat(raw_expires)
        except ValueError as exc:
            raise CorruptedIdempotencyRecordError(record.key) from exc
    checkout_status: CheckoutStatus = raw_status
    return CheckoutResult(
        transaction_id=transaction_id,
        status=checkout_status,
        payment_url=url,
        expires_at=expires_at,
    )


def _selected_slot_ids(tx: Transaction, payment_id: str) -> list[int]:
    raw = tx.selected_slot_ids
    if not isinstance(raw, list) or not all(isinstance(item, int) for item in raw):
        raise UnlinkedPaymentError(
            payment_id, f"selected_slot_ids транзакции {tx.pk} повреждён"
        )
    return [int(item) for item in raw]


def _month_after(day: date) -> date:
    year = day.year + day.month // 12
    month = day.month % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return day.replace(year=year, month=month, day=min(day.day, last_day))


def _first_lessons(slot_ids: list[int], schedule_port: SchedulePort) -> dict[int, date]:
    # ПОЧЕМУ «сейчас» — момент активации, а не оформления заказа (решение
    # бизнеса 2026-10-01): вебхук, пришедший после начала занятия, сдвигает
    # старт на следующее — абонемент никогда не начинается с прошедшего
    now = timezone.now()
    return {
        slot_id: schedule_port.get_next_lesson_date(slot_id, now)
        for slot_id in sorted(slot_ids)
    }


def _activation_window(first_lessons: dict[int, date]) -> tuple[date, datetime]:
    # ПОЧЕМУ: согласно checkout-flow.md, абонемент действует строго месяц
    # начиная с даты первого фактического занятия в выбранных слотах
    first_lesson = min(first_lessons.values())
    expires_at = timezone.make_aware(
        datetime.combine(_month_after(first_lesson), time(23, 59, 59))
    )
    return first_lesson, expires_at


def _join_todays_lessons(subscription_id: int, first_lessons: dict[int, date]) -> None:
    # ПОЧЕМУ: журнал дня собирается в 07:00 (journal.materialize_today_lessons);
    # купивший днём до начала занятия иначе не попал бы в него до следующей недели
    today = timezone.localdate()
    todays_slots = [slot_id for slot_id, day in first_lessons.items() if day == today]
    if not todays_slots:
        return
    enrollment_ids = Enrollment.objects.filter(
        subscription_id=subscription_id,
        schedule_id__in=todays_slots,
        status=EnrollmentStatus.ENROLLED,
    ).values_list("pk", flat=True)
    _add_to_journal(dict.fromkeys(enrollment_ids, today))


def _add_to_journal(lesson_dates: dict[int, date]) -> None:
    # ПОЧЕМУ так же, как journal.open_lesson: посещаемость автоматическая,
    # педагог только снимает отсутствующих. ignore_conflicts опирается на
    # uq_billing_attendance_per_enrollment_date — повтор вебхука и утренняя
    # задача журнала не задвоят отметку и не тронут выставленную педагогом
    Attendance.objects.bulk_create(
        [
            Attendance(
                enrollment_id=enrollment_id,
                date=lesson_date,
                status=AttendanceStatus.ATTENDED,
            )
            for enrollment_id, lesson_date in lesson_dates.items()
        ],
        ignore_conflicts=True,
    )


class CheckoutTransactionNotFoundError(BillingError):
    # ПОЧЕМУ одна ошибка на «нет» и «чужая»: чужое неотличимо от несуществующего,
    # иначе перебором id можно узнать, что транзакция есть
    def __init__(self, transaction_id: uuid.UUID) -> None:
        super().__init__(f"Транзакция {transaction_id} не найдена.")
        self.transaction_id = transaction_id


CheckoutOutcomeStatus = Literal["PENDING", "SUCCEEDED", "CANCELED", "REFUND"]
CheckoutOrderType = Literal["SUBSCRIPTION", "TRIAL"]
RefundReason = Literal[
    "SEATS_TAKEN",
    "GROUP_CLOSED",
    "PAID_AFTER_EXPIRY",
    "AMOUNT_MISMATCH",
    "NOT_FULFILLED",
]

# ПОЧЕМУ: внутренние причины (metadata) сводятся к короткому набору для фронта —
# новая внутренняя причина не ломает экран, а падает в NOT_FULFILLED
_REFUND_REASONS: dict[str, RefundReason] = {
    "SEATS_TAKEN_AFTER_PAYMENT": "SEATS_TAKEN",
    # ПОЧЕМУ не SEATS_TAKEN: бронь снимает только чистка протухших (>15 мин)
    # на чужом чекауте — родитель заплатил поздно, а места могут быть свободны
    "HOLD_LOST": "PAID_AFTER_EXPIRY",
    "SLOT_REMOVED": "GROUP_CLOSED",
    "PAYMENT_SUCCEEDED_AFTER_EXPIRY": "PAID_AFTER_EXPIRY",
    "AMOUNT_MISMATCH": "AMOUNT_MISMATCH",
}

# ПОЧЕМУ: страница результата опрашивает статус раз в пару секунд. Если вебхук
# опоздал (локально не доходит вовсе), просим воркер спросить ЮКассу сами —
# но не сразу (вебхук обычно успевает за секунды) и не чаще интервала, иначе
# опрос превратится в шквал запросов к ЮКассе. Отсчёт — от первого опроса,
# а не от created_at: к возврату со страницы оплаты заказу уже минуты
_PAYMENT_RECHECK_GRACE = timedelta(seconds=10)
_PAYMENT_RECHECK_INTERVAL = timedelta(seconds=15)


@dataclass(frozen=True)
class CheckoutOrderSlot:
    schedule_id: int
    activity_name: str
    group_name: str
    day_of_week: int
    start_time: time
    end_time: time


@dataclass(frozen=True)
class CheckoutOrder:
    title: str
    student_name: str
    trial_date: date | None
    slots: list[CheckoutOrderSlot]


@dataclass(frozen=True)
class CheckoutOutcome:
    id: uuid.UUID
    type: CheckoutOrderType
    status: CheckoutOutcomeStatus
    reason: RefundReason | None
    amount: int
    created_at: datetime
    expires_at: datetime
    order: CheckoutOrder


def get_checkout_outcome(
    transaction_id: uuid.UUID, parent_id: int, *, now: datetime | None = None
) -> CheckoutOutcome:
    """Итог оплаты глазами родителя — для страницы «результат оплаты».

    Отдаёт исход, а не сырой статус строки: оплаченное пробное, на которое
    не хватило места, в БД SUCCEEDED, а для родителя это возврат.
    """
    tx = (
        Transaction.objects.select_related(
            "subscription__plan",
            "enrollment__student",
            "enrollment__schedule__activity",
        )
        .filter(pk=transaction_id, parent_id=parent_id)
        .first()
    )
    if tx is None:
        raise CheckoutTransactionNotFoundError(transaction_id)

    outcome = _parent_outcome(tx)
    if outcome == "PENDING":
        _request_payment_recheck(tx, now if now is not None else timezone.now())
    return CheckoutOutcome(
        id=tx.pk,
        type="TRIAL" if tx.enrollment_id is not None else "SUBSCRIPTION",
        status=outcome,
        reason=_refund_reason(tx) if outcome == "REFUND" else None,
        # ПОЧЕМУ: сколько реально прошло через карту — оно же вернётся при
        # возврате; до оплаты — сколько предстоит заплатить
        amount=tx.received_amount if tx.received_amount is not None else tx.amount,
        created_at=tx.created_at,
        expires_at=tx.created_at + _PENDING_TRANSACTION_TTL,
        order=_checkout_order(tx),
    )


def _parent_outcome(tx: Transaction) -> CheckoutOutcomeStatus:
    # !!!: возврат проверяется первым и не по одному флагу. requires_compensation
    # значит «возврат ещё надо отправить» и сбрасывается при отправке
    # (issue_pending_refunds) — дальше о возврате говорит refund_status.
    # FAILED — деньги пришли, заказ не исполнен: это всегда возврат
    if (
        tx.requires_compensation
        or tx.refund_status is not None
        or tx.status == TransactionStatus.FAILED
    ):
        return "REFUND"
    if tx.status == TransactionStatus.PENDING:
        return "PENDING"
    if tx.status == TransactionStatus.SUCCEEDED:
        return "SUCCEEDED"
    return "CANCELED"


def _refund_reason(tx: Transaction) -> RefundReason:
    # ПОЧЕМУ failure_reason первым: у несовпадения суммы ключа reason нет
    code = tx.metadata.get("failure_reason") or tx.metadata.get("reason")
    return _REFUND_REASONS.get(str(code), "NOT_FULFILLED")


def _checkout_order(tx: Transaction) -> CheckoutOrder:
    if tx.enrollment is not None:
        trial = tx.enrollment
        return CheckoutOrder(
            title="Пробное занятие",
            student_name=trial.student.full_name,
            trial_date=trial.trial_date,
            slots=[_order_slot(trial)],
        )
    subscription = tx.subscription
    if subscription is None:
        return CheckoutOrder(title="", student_name="", trial_date=None, slots=[])
    # ПОЧЕМУ все записи абонемента, а не только живые: после возврата они
    # CANCELED, но родителю всё равно надо видеть, за что он платил
    enrollments = list(
        Enrollment.objects.filter(subscription_id=subscription.pk)
        .select_related("student", "schedule__activity")
        .order_by("schedule__day_of_week", "schedule__start_time", "pk")
    )
    return CheckoutOrder(
        title=f"Абонемент «{subscription.plan.name}»",
        student_name=enrollments[0].student.full_name if enrollments else "",
        trial_date=None,
        slots=[_order_slot(enrollment) for enrollment in enrollments],
    )


def _order_slot(enrollment: Enrollment) -> CheckoutOrderSlot:
    schedule = enrollment.schedule
    return CheckoutOrderSlot(
        schedule_id=schedule.pk,
        activity_name=schedule.activity.name,
        group_name=schedule.group_name,
        day_of_week=schedule.day_of_week,
        start_time=schedule.start_time,
        end_time=schedule.end_time,
    )


def _request_payment_recheck(tx: Transaction, moment: datetime) -> None:
    # !!!: здесь только постановка задачи — никакого похода в ЮКассу внутри
    # HTTP-запроса. Задача та же, что у вебхука (verify_and_process_payment),
    # а её путь идемпотентен: строка под select_for_update, работа только из
    # PENDING. Повтор стоит одного GET к ЮКассе, деньги и места не трогает
    if tx.external_id is None:
        return  # платёж у провайдера не заведён — спрашивать нечего
    payment_id = tx.external_id
    now_ts = moment.timestamp()
    try:
        first_seen = cache.get_or_set(
            f"billing:payment-recheck:seen:{tx.pk}",
            now_ts,
            timeout=int(_RECONCILE_GIVE_UP_AFTER.total_seconds()),
        )
        if not isinstance(first_seen, float):
            first_seen = now_ts
        if now_ts - first_seen < _PAYMENT_RECHECK_GRACE.total_seconds():
            return
        # ПОЧЕМУ add: атомарный SET NX в Redis — из параллельных опросов
        # задачу ставит ровно один, остальные до конца интервала молчат
        if not cache.add(
            f"billing:payment-recheck:lock:{tx.pk}",
            1,
            timeout=int(_PAYMENT_RECHECK_INTERVAL.total_seconds()),
        ):
            return
    except Exception:
        # ПОЧЕМУ: без кэша не можем ограничить частоту — лучше не проверить,
        # чем завалить ЮКассу; статус родителю всё равно отдаём
        logger.warning(
            "Досрочная сверка %s: кэш недоступен, пропускаем.", tx.pk, exc_info=True
        )
        return
    db_transaction.on_commit(lambda: _enqueue_payment_verification(payment_id))


def _enqueue_payment_verification(payment_id: str) -> None:
    # ПОЧЕМУ: локальный импорт — tasks импортирует services на уровне модуля
    from apps.billing import tasks

    kiq_safely(tasks.verify_and_process_payment, payment_id)


def sweep_stale_pending_transactions(
    *,
    gateway: PaymentGateway,
    schedule_port: SchedulePort,
    now: datetime | None = None,
) -> int:
    # ПОЧЕМУ: защита от зависания забронированных слотов, если вебхук
    # от провайдера потерялся или клиент бросил оплату на полпути
    # !!!: заказ, заведённый в ЮКассе, снимается только после сверки с ней —
    # вебхук мог потеряться, а деньги прийти. Молча снятый оплаченный заказ
    # никто бы не вернул: возврат запускает только опоздавший вебхук
    moment = now if now is not None else timezone.now()
    cutoff = moment - _PENDING_TRANSACTION_TTL
    give_up_before = moment - _RECONCILE_GIVE_UP_AFTER
    # ПОЧЕМУ: LIMIT без ORDER BY недетерминирован — при переполнении чанка
    # свипер обязан снимать самые старые брони первыми
    stale = list(
        Transaction.objects.filter(
            status=TransactionStatus.PENDING, created_at__lt=cutoff
        )
        .order_by("created_at")
        .values_list("pk", "external_id", "created_at")[:_SWEEP_CHUNK_SIZE]
    )

    swept = 0
    reconcile_budget = _RECONCILE_CHUNK_SIZE
    gateway_down = False
    for tx_id, external_id, created_at in stale:
        # ПОЧЕМУ: без external_id платёж у провайдера не заведён (или процесс
        # умер до сохранения id — тогда клиент не получил ссылку): спрашивать
        # некого и платить нечем, снимаем как раньше
        gave_up = external_id is not None and created_at < give_up_before
        if external_id is not None and not gave_up:
            # ПОЧЕМУ: заказ, не сверенный в этом тике, остаётся PENDING —
            # сверим в следующем, а не снимаем вслепую
            if gateway_down or reconcile_budget == 0:
                continue
            reconcile_budget -= 1
            try:
                settled = _reconcile_stale_payment(
                    tx_id, external_id, gateway, schedule_port
                )
            except GatewayError as exc:
                # ПОЧЕМУ: ЮКасса лежит или отвергает наши запросы — остальные
                # запросы тика упрутся в то же; прекращаем сверку до следующего тика
                _log_reconcile_postponed(tx_id, exc)
                gateway_down = True
                continue
            if settled:
                continue

        if _expire_pending_transaction(
            tx_id, recheck_until=moment + _POST_EXPIRY_RECHECK_WINDOW
        ):
            swept += 1
            if gave_up:
                logger.critical(
                    "Сверка %s: платёж %s не удалось сверить за %s — заказ снят "
                    "без сверки; досверка продолжится.",
                    tx_id,
                    external_id,
                    _RECONCILE_GIVE_UP_AFTER,
                )

    # ПОЧЕМУ: досверке снятых заказов — остаток бюджета тика. Снятие по TTL
    # важнее: оно держит места в группах
    _close_overdue_rechecks(moment)
    if not gateway_down and reconcile_budget > 0:
        _recheck_expired_payments(gateway, schedule_port, moment, reconcile_budget)
    return swept


def _log_reconcile_postponed(tx_id: uuid.UUID, exc: GatewayError) -> None:
    if isinstance(exc, GatewayNetworkError):
        logger.warning(
            "Сверка %s: ЮКасса недоступна, сверка отложена до следующего тика.",
            tx_id,
            exc_info=exc,
        )
        return
    logger.critical(
        "Сверка %s: нарушение контракта API ЮКассы (ключи/схема ответа) — "
        "сверка отложена до следующего тика.",
        tx_id,
        exc_info=exc,
    )


def _reconcile_stale_payment(
    tx_id: uuid.UUID,
    payment_id: str,
    gateway: PaymentGateway,
    schedule_port: SchedulePort,
) -> bool:
    # ПОЧЕМУ: True — провайдер дал окончательный статус, и он проведён тем же
    # путём, что и вебхук; False — снимать заказ по TTL локально.
    # !!!: сетевой вызов строго вне транзакции и локов (контракт apps.billing.ports);
    # запись — отдельной короткой транзакцией внутри _apply_verified_payment.
    # Смерть процесса между ними безопасна: GET идемпотентен, следующий тик
    # спросит заново
    try:
        info = gateway.get_payment(payment_id)
    except (PaymentNotFoundError, InvalidPaymentIdError):
        logger.error(
            "Сверка %s: платёж %s неизвестен ЮКассе — денег нет, заказ снимается "
            "по TTL.",
            tx_id,
            payment_id,
        )
        return False

    if info.status not in ("succeeded", "canceled"):
        # ПОЧЕМУ: pending/waiting_for_capture — за TTL не оплачено. Заказ снимается,
        # места освобождаются; оплату позже поймает вебхук или досверка
        # (_recheck_expired_payments) и отправит в возврат
        return False

    if info.transaction_id != str(tx_id):
        logger.error(
            "Сверка %s: платёж %s ссылается на транзакцию %r — заказ снимается "
            "по TTL, требуется ручной разбор.",
            tx_id,
            payment_id,
            info.transaction_id,
        )
        return False

    # ПОЧЕМУ: вебхук мог успеть за время GET — тогда он уже всё провёл,
    # и тревога «вебхук не дошёл» была бы ложной
    if not Transaction.objects.filter(
        pk=tx_id, status=TransactionStatus.PENDING
    ).exists():
        return True

    if info.status == "succeeded":
        # ПОЧЕМУ critical: оплата без вебхука — признак, что вебхуки ЮКассы
        # до нас не доходят (URL, allowlist за прокси); чинить надо источник
        logger.critical(
            "Сверка %s: платёж %s оплачен, но вебхук не пришёл — проведён "
            "сверкой. Проверьте доставку вебхуков ЮКассы.",
            tx_id,
            payment_id,
        )
    try:
        _apply_verified_payment(info, tx_id, schedule_port)
    except BillingError:
        # ПОЧЕМУ: как в run_payment_verification — бизнес-исход (компенсация,
        # несовпадение суммы) уже закоммичен, ошибка лишь сообщает о нём
        logger.exception(
            "Сверка %s: платёж %s проведён с бизнес-ошибкой — требуется ручной разбор.",
            tx_id,
            payment_id,
        )
    return True


def _expire_pending_transaction(tx_id: uuid.UUID, *, recheck_until: datetime) -> bool:
    with db_transaction.atomic():
        tx = (
            Transaction.objects.select_for_update()
            .filter(pk=tx_id, status=TransactionStatus.PENDING)
            .first()
        )
        if tx is None:
            return False  # вебхук успел первым — не трогаем
        tx.status = TransactionStatus.CANCELED
        tx.metadata = {**tx.metadata, "canceled_reason": _TTL_EXPIRED_REASON}
        # ПОЧЕМУ: платёж заведён в ЮКассе — родитель ещё может оплатить по
        # старой ссылке, ставим заказ в очередь досверки
        if tx.external_id is not None:
            tx.payment_recheck_until = recheck_until
        tx.save(update_fields=["status", "metadata", "payment_recheck_until"])
        _release_order_resources(tx)
        return True


def _recheck_expired_payments(
    gateway: PaymentGateway,
    schedule_port: SchedulePort,
    moment: datetime,
    budget: int,
) -> None:
    # ПОЧЕМУ: «спросить перед снятием» не ловит оплату, случившуюся после
    # снятия: вебхук потерян — возврата нет. Досверка спрашивает ЮКассу, пока
    # та не даст окончательный статус (неоплаченный платёж она отменяет сама —
    # expired_on_confirmation). Раньше снятые — первыми
    # ПОЧЕМУ верхняя граница: заказ, снятый в этом же тике, получил срок ровно
    # moment + окно — его только что спросили, повторный GET бессмыслен
    due = list(
        Transaction.objects.filter(
            payment_recheck_until__gte=moment,
            payment_recheck_until__lt=moment + _POST_EXPIRY_RECHECK_WINDOW,
        )
        .order_by("payment_recheck_until")
        .values_list("pk", "external_id")[:budget]
    )
    for tx_id, external_id in due:
        if external_id is None:
            _finish_recheck(tx_id)
            continue
        try:
            final = _recheck_expired_payment(tx_id, external_id, gateway, schedule_port)
        except GatewayError as exc:
            _log_reconcile_postponed(tx_id, exc)
            return
        if final:
            _finish_recheck(tx_id)


def _recheck_expired_payment(
    tx_id: uuid.UUID,
    payment_id: str,
    gateway: PaymentGateway,
    schedule_port: SchedulePort,
) -> bool:
    # ПОЧЕМУ: True — статус окончательный, досверку закрываем; False —
    # платёж ещё открыт, спросим в следующем тике.
    # !!!: как и сверка — сеть вне транзакции, запись в _apply_verified_payment.
    # Смерть процесса до _finish_recheck безопасна: следующий тик увидит, что
    # возврат уже поставлен, и просто закроет досверку
    try:
        info = gateway.get_payment(payment_id)
    except (PaymentNotFoundError, InvalidPaymentIdError):
        logger.error(
            "Досверка %s: платёж %s неизвестен ЮКассе — досверка закрыта.",
            tx_id,
            payment_id,
        )
        return True

    if info.status == "canceled":
        return True
    if info.status != "succeeded":
        return False

    if info.transaction_id != str(tx_id):
        logger.error(
            "Досверка %s: платёж %s ссылается на транзакцию %r — досверка закрыта, "
            "требуется ручной разбор.",
            tx_id,
            payment_id,
            info.transaction_id,
        )
        return True

    # ПОЧЕМУ: вебхук мог успеть — оплата уже учтена (возврат поставлен,
    # выплачен или ушёл на ручной разбор). Маркер тот же, что в _apply_success:
    # received_amount заполняется при первом же учёте пришедших денег
    tx = Transaction.objects.only("status", "received_amount").get(pk=tx_id)
    if tx.status != TransactionStatus.CANCELED or tx.received_amount is not None:
        return True

    logger.critical(
        "Досверка %s: платёж %s оплачен после снятия заказа, вебхук не пришёл — "
        "поставлен возврат. Проверьте доставку вебхуков ЮКассы.",
        tx_id,
        payment_id,
    )
    try:
        _apply_verified_payment(info, tx_id, schedule_port)
    except PaymentSucceededAfterExpiryError:
        pass  # ожидаемый исход: заказ не восстанавливается, возврат поставлен
    except BillingError:
        logger.exception(
            "Досверка %s: платёж %s проведён с бизнес-ошибкой — требуется ручной "
            "разбор.",
            tx_id,
            payment_id,
        )
    return True


def _finish_recheck(tx_id: uuid.UUID) -> bool:
    # ПОЧЕМУ: условный UPDATE — параллельный тик, закрывший досверку первым,
    # не получит второй записи и второго алерта
    return (
        Transaction.objects.filter(
            pk=tx_id, payment_recheck_until__isnull=False
        ).update(payment_recheck_until=None)
        == 1
    )


def _close_overdue_rechecks(moment: datetime) -> None:
    overdue = list(
        Transaction.objects.filter(payment_recheck_until__lt=moment)
        .order_by("payment_recheck_until")
        .values_list("pk", "external_id")[:_SWEEP_CHUNK_SIZE]
    )
    for tx_id, external_id in overdue:
        if _finish_recheck(tx_id):
            logger.critical(
                "Досверка %s: платёж %s не получил окончательного статуса за %s — "
                "проверьте оплату в кабинете ЮКассы.",
                tx_id,
                external_id,
                _POST_EXPIRY_RECHECK_WINDOW,
            )


def sweep_expired_subscriptions(*, now: datetime | None = None) -> int:
    # ПОЧЕМУ: автоматический перевод протухших абонементов в EXPIRED
    # с расчетом и начислением несгораемого остатка на депозит
    moment = now if now is not None else timezone.now()
    candidate_ids = list(
        Subscription.objects.filter(
            status=SubscriptionStatus.ACTIVE, expires_at__lt=moment
        )
        .order_by("expires_at")
        .values_list("pk", flat=True)[:_SWEEP_CHUNK_SIZE]
    )

    swept = 0
    for subscription_id in candidate_ids:
        with db_transaction.atomic():
            subscription = (
                Subscription.objects.select_for_update()
                .select_related("plan")
                .filter(
                    pk=subscription_id,
                    status=SubscriptionStatus.ACTIVE,
                    expires_at__lt=moment,
                )
                .first()
            )
            if subscription is None:
                continue  # конкурентный тик успел первым
            # !!!: до расчёта остатка и до отмены записей — иначе занятие
            # последнего дня, не списанное ночной задачей, вернулось бы деньгами
            _debit_attended_before_expiry(subscription)
            subscription.status = SubscriptionStatus.EXPIRED
            subscription.save(update_fields=["status"])
            Enrollment.objects.filter(
                subscription=subscription,
                status__in=(EnrollmentStatus.HELD, EnrollmentStatus.ENROLLED),
            ).update(status=EnrollmentStatus.CANCELED)
            _credit_unused_sessions(subscription)
            swept += 1
    return swept


def _debit_attended_before_expiry(subscription: Subscription) -> None:
    # ПОЧЕМУ: страховка ночной задачи (journal.debit_attended_lessons) —
    # упала, не успела до 23:59:59 последнего дня или отметку вернули в
    # «пришёл» после неё. debit_token здесь не подходит: срок по дате уже
    # вышел, а блокировка абонемента у свипера и так взята
    assert subscription.expires_at is not None  # сужение: свипер фильтрует по нему
    last_day = timezone.localdate(subscription.expires_at)
    attendances = (
        attendances_awaiting_debit()
        .filter(enrollment__subscription=subscription, date__lte=last_day)
        .select_related("enrollment")
        .select_for_update(of=("self",))
        .order_by("pk")
    )
    for attendance in attendances:
        try:
            _spend_slot_token(attendance, subscription.pk)
        except (InsufficientTokensError, SlotBalanceNotFoundError) as exc:
            # ПОЧЕМУ: пятое занятие месяца при 4 фишках бесплатно (решение
            # бизнеса) — не причина срывать закрытие абонемента
            logger.info("Отметка #%s не списана при истечении: %s", attendance.pk, exc)


def _credit_unused_sessions(subscription: Subscription) -> int:
    # !!!: строгий SELECT FOR UPDATE обязателен, иначе есть риск
    # начислить возврат по грязным данным до коммита списания фишки
    slots = list(
        SubscriptionSlot.objects.select_for_update()
        .filter(subscription=subscription)
        .order_by("pk")
    )
    granted_sessions = sum(slot.granted_tokens for slot in slots)
    used_sessions = max(
        0, granted_sessions - sum(slot.remaining_tokens for slot in slots)
    )
    price = subscription.purchase_price
    credit = max(0, min(price - used_sessions * subscription.base_session_price, price))

    SubscriptionSlot.objects.filter(subscription=subscription).update(
        remaining_tokens=0
    )

    if credit == 0:
        return 0

    deposit = _locked_parent_deposit(subscription.parent_id)
    try:
        DepositEntry.objects.create(
            deposit=deposit,
            amount=credit,
            reason=DepositEntryReason.SUBSCRIPTION_EXPIRY_CREDIT,
            subscription=subscription,
        )
    except IntegrityError:
        # ПОЧЕМУ: начисление выполнено параллельно, защита БД сработала
        return 0
    deposit.balance += credit
    deposit.save(update_fields=["balance", "updated_at"])
    return credit


def _locked_parent_deposit(parent_id: int) -> ParentDeposit:
    # ПОЧЕМУ: get_or_create с select_for_update не защищает от гонок при вставке,
    # перехватываем IntegrityError явно перед захватом блокировки
    if not ParentDeposit.objects.filter(parent_id=parent_id).exists():
        with suppress(IntegrityError), db_transaction.atomic():
            ParentDeposit.objects.create(parent_id=parent_id)
    return ParentDeposit.objects.select_for_update().get(parent_id=parent_id)


def issue_pending_refunds(
    *, gateway: PaymentGateway, now: datetime | None = None
) -> int:
    moment = now if now is not None else timezone.now()
    candidate_ids = list(
        Transaction.objects.filter(requires_compensation=True)
        .order_by("created_at")
        .values_list("pk", flat=True)[:_REFUND_CHUNK_SIZE]
    )

    issued = 0
    for tx_id in candidate_ids:
        # ПОЧЕМУ (claim check): возврат резервируется в БД ДО похода в сеть —
        # параллельный тик (дубль крона, ручной запуск из админки) не отправит
        # второй create_refund. Lease с TTL, а не вечный флаг: воркер, убитый
        # после сетевого вызова, не хоронит возврат — после истечения lease
        # повтор дедуплицируется Idempotence-Key провайдера
        claimed = Transaction.objects.filter(
            Q(compensation_claimed_until__isnull=True)
            | Q(compensation_claimed_until__lt=moment),
            pk=tx_id,
            requires_compensation=True,
        ).update(compensation_claimed_until=moment + _REFUND_CLAIM_TTL)
        if claimed == 0:
            continue

        tx = Transaction.objects.get(pk=tx_id)
        # ПОЧЕМУ: external_id проставляется уже на чекауте (платёж заведён у
        # провайдера), поэтому сам по себе он больше не доказывает, что деньги
        # получены. Факт оплаты подтверждает только уход из PENDING: вебхук
        # переводит транзакцию в SUCCEEDED/FAILED, поздний успех — в CANCELED
        if tx.external_id is None or tx.status == TransactionStatus.PENDING:
            _quarantine_refund(
                tx_id, "платёж не подтверждён провайдером — возвращать нечего"
            )
            continue

        # ПОЧЕМУ: возвращаем пришедшее, а не ожидаемое — при недоплате ЮКасса
        # отвергла бы возврат больше суммы платежа. Сумма берётся из колонки,
        # поэтому ретрай после падения шлёт тот же запрос под тем же ключом.
        # NULL — транзакции до появления колонки, там сумма сверена с amount
        refund_amount = (
            tx.received_amount if tx.received_amount is not None else tx.amount
        )
        try:
            refund = gateway.create_refund(
                payment_id=tx.external_id,
                amount_kopecks=refund_amount,
                idempotence_key=f"refund-{tx.pk}",
            )
        except GatewayContractError as exc:
            _quarantine_refund(tx_id, str(exc))
            continue
        # ПОЧЕМУ: транзитные сбои (GatewayNetworkError) пробрасываются наружу
        # для автоматического ретрая на уровне Taskiq

        with db_transaction.atomic():
            locked = Transaction.objects.select_for_update().get(pk=tx_id)
            if not locked.requires_compensation:
                continue
            locked.requires_compensation = False
            locked.compensation_claimed_until = None
            locked.refund_id = refund.id
            locked.refund_status = RefundStatus(refund.status.upper())
            locked.metadata = {**locked.metadata, "compensation_required": False}
            locked.save(
                update_fields=[
                    "requires_compensation",
                    "compensation_claimed_until",
                    "refund_id",
                    "refund_status",
                    "metadata",
                ]
            )
            if locked.refund_status == RefundStatus.SUCCEEDED:
                _schedule_refund_email(locked.pk)
            issued += 1
    return issued


def sync_pending_refunds(*, gateway: PaymentGateway) -> int:
    # ПОЧЕМУ: ЮКасса может принять возврат в обработку и позже отменить его.
    # Итог узнаём опросом API, а не из вебхука: тело вебхука не доверенное,
    # и повторный GET по тому же возврату идемпотентен
    candidates = list(
        Transaction.objects.filter(
            refund_status=RefundStatus.PENDING, refund_id__isnull=False
        )
        .order_by("created_at")
        .values_list("pk", "refund_id")[:_REFUND_CHUNK_SIZE]
    )

    changed = 0
    for tx_id, refund_id in candidates:
        assert refund_id is not None  # сужение: выборка фильтрует NULL
        error: str | None = None
        try:
            refund = gateway.get_refund(refund_id)
        except GatewayContractError as exc:
            new_status, error = RefundStatus.FAILED, str(exc)
        else:
            if refund.status == "pending":
                continue
            new_status = RefundStatus(refund.status.upper())
        # ПОЧЕМУ: транзитные сбои (GatewayNetworkError) пробрасываются наружу —
        # незавершённые возвраты доопросит ретрай или следующий тик

        with db_transaction.atomic():
            locked = (
                Transaction.objects.select_for_update()
                .filter(pk=tx_id, refund_status=RefundStatus.PENDING)
                .first()
            )
            if locked is None:
                continue  # параллельный тик успел первым
            locked.refund_status = new_status
            if error is not None:
                locked.metadata = {**locked.metadata, "refund_error": error}
            locked.save(update_fields=["refund_status", "metadata"])
            if new_status == RefundStatus.SUCCEEDED:
                _schedule_refund_email(locked.pk)
            else:
                _schedule_refund_review_alert(locked.pk)
            changed += 1
    return changed


def _schedule_refund_email(transaction_id: uuid.UUID) -> None:
    # ПОЧЕМУ: письмо только о выполненном возврате — обещать деньги, пока
    # ЮКасса ещё может отменить возврат, нельзя
    db_transaction.on_commit(
        lambda: _enqueue_refund_task("send_refund_email_task", transaction_id)
    )


def _schedule_refund_review_alert(transaction_id: uuid.UUID) -> None:
    # ПОЧЕМУ: экран ручного разбора сам никого не зовёт — без сообщения
    # менеджерам такой возврат ждал бы, пока кто-то случайно откроет админку
    db_transaction.on_commit(
        lambda: _enqueue_refund_task("notify_refund_review_task", transaction_id)
    )


def _enqueue_refund_task(task_name: str, transaction_id: uuid.UUID) -> None:
    # ПОЧЕМУ: локальный импорт — tasks импортирует services на уровне модуля.
    # Вызывается из on_commit: без него воркер может прочитать транзакцию
    # раньше коммита
    from apps.billing import tasks

    kiq_safely(getattr(tasks, task_name), str(transaction_id))


def _quarantine_refund(tx_id: uuid.UUID, reason: str) -> None:
    with db_transaction.atomic():
        locked = Transaction.objects.select_for_update().get(pk=tx_id)
        if not locked.requires_compensation:
            return
        locked.requires_compensation = False
        locked.compensation_claimed_until = None
        locked.refund_status = RefundStatus.FAILED
        locked.metadata = {
            **locked.metadata,
            "compensation_required": False,
            "refund_error": reason,
        }
        locked.save(
            update_fields=[
                "requires_compensation",
                "compensation_claimed_until",
                "refund_status",
                "metadata",
            ]
        )
        _schedule_refund_review_alert(locked.pk)


def resolve_refund_manually(transaction_id: uuid.UUID, *, resolved_by: str) -> None:
    # ПОЧЕМУ: менеджер разобрал возврат вне системы (кабинет ЮКассы) — фиксируем,
    # кто и когда закрыл. Прежние refund_error/refund_id остаются для аудита
    with db_transaction.atomic():
        tx = Transaction.objects.select_for_update().filter(pk=transaction_id).first()
        if (
            tx is None
            or tx.requires_compensation
            or tx.refund_status not in REFUND_STATUSES_AWAITING_MANUAL
        ):
            raise RefundNotAwaitingManualError(transaction_id)
        tx.refund_status = RefundStatus.MANUAL
        tx.metadata = {
            **tx.metadata,
            "refund_resolved_by": resolved_by,
            "refund_resolved_at": timezone.now().isoformat(),
        }
        tx.save(update_fields=["refund_status", "metadata"])


def sweep_finalized_idempotency_records(*, now: datetime | None = None) -> int:
    # ПОЧЕМУ: контракт §2.1, записи идемпотентности старше суток
    # уничтожаются для освобождения места в БД
    moment = now if now is not None else timezone.now()
    cutoff = moment - _IDEMPOTENCY_RECORD_TTL
    deleted, _ = (
        IdempotencyRecord.objects.filter(created_at__lt=cutoff)
        .filter(Q(locked_until__isnull=True) | Q(locked_until__lt=cutoff))
        .delete()
    )
    return deleted


# Административные операции (Django Admin)


class InvalidFreezePeriodError(BillingError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SubscriptionNotFreezableError(BillingError):
    def __init__(self, subscription_id: int, status: str) -> None:
        super().__init__(
            f"Абонемент id={subscription_id} (status={status}) нельзя заморозить: "
            "требуется ACTIVE с установленным expires_at."
        )
        self.subscription_id = subscription_id
        self.status = status


class TokenNotRefundableError(BillingError):
    def __init__(self, attendance_id: int, reason: str) -> None:
        super().__init__(
            f"Возврат фишки по отметке id={attendance_id} невозможен: {reason}"
        )
        self.attendance_id = attendance_id
        self.reason = reason


@dataclass(frozen=True, slots=True)
class BulkFreezeResult:
    frozen_count: int
    frozen_days: int
    errors: list[str]


def bulk_freeze_subscriptions(
    *,
    subscription_ids: Sequence[int],
    start_date: date,
    end_date: date,
    reason: str,
) -> BulkFreezeResult:
    if end_date <= start_date:
        raise InvalidFreezePeriodError(
            "Дата окончания заморозки должна быть позже даты начала."
        )
    if not reason.strip():
        raise InvalidFreezePeriodError("Причина заморозки обязательна.")
    if not subscription_ids:
        raise InvalidFreezePeriodError("Не выбрано ни одного абонемента.")

    frozen_days = (end_date - start_date).days
    shift = timedelta(days=frozen_days)
    errors: list[str] = []

    with db_transaction.atomic():
        # ПОЧЕМУ: одна транзакция и одна блокировка на весь пакет вместо
        # O(N) отдельных get()+UPDATE, иначе экшен на 50 строк съедает
        # пул соединений и ловит дедлоки
        # ПОЧЕМУ: без ORDER BY два пересекающихся пакета заморозки лочат
        # строки в разном порядке и ловят взаимный дедлок
        subscriptions = list(
            Subscription.objects.select_for_update()
            .filter(pk__in=subscription_ids)
            .order_by("pk")
        )

        found_ids = {subscription.pk for subscription in subscriptions}
        for missing_id in set(subscription_ids) - found_ids:
            errors.append(f"Абонемент #{missing_id}: не найден.")

        to_update: list[Subscription] = []
        for subscription in subscriptions:
            if (
                subscription.status != SubscriptionStatus.ACTIVE
                or subscription.expires_at is None
            ):
                errors.append(
                    f"Абонемент #{subscription.pk}: заморозить можно только "
                    f"активный с датой истечения (сейчас {subscription.status})."
                )
                continue
            subscription.expires_at += shift
            to_update.append(subscription)

        if to_update:
            Subscription.objects.bulk_update(to_update, ["expires_at"])

    # TODO: завести таблицу subscription_freeze (журнал заморозок с reason,
    # performed_by) — сейчас причина фиксируется только в логах
    logger.info(
        "Frozen %s subscriptions for %s days (%s — %s): %s",
        len(to_update),
        frozen_days,
        start_date,
        end_date,
        reason,
    )
    return BulkFreezeResult(
        frozen_count=len(to_update), frozen_days=frozen_days, errors=errors
    )


def refund_token(attendance_id: int) -> None:
    # !!!: зеркало debit_token — обязано оставаться идемпотентным
    # и работать под теми же блокировками
    with db_transaction.atomic():
        try:
            attendance = (
                Attendance.objects.select_for_update(of=("self",))
                .select_related("enrollment__subscription")
                .get(pk=attendance_id)
            )
        except Attendance.DoesNotExist as exc:
            raise AttendanceNotFoundError(attendance_id) from exc

        if not attendance.token_debited:
            return

        subscription = attendance.enrollment.subscription
        # ПОЧЕМУ: у пробного нет абонемента и фишек — списания не было,
        # возвращать нечего (сюда попасть можно только при порче данных)
        if subscription is None:
            return
        # ПОЧЕМУ: возврат на не-ACTIVE запрещен — sweep_expired_subscriptions
        # уже обнулил остатки и начислил несгораемый остаток на депозит,
        # инкремент remaining_tokens задним числом разъехался бы с учетом
        if subscription.status != SubscriptionStatus.ACTIVE:
            raise TokenNotRefundableError(
                attendance_id,
                f"абонемент id={subscription.pk} в статусе {subscription.status}.",
            )

        try:
            slot = SubscriptionSlot.objects.select_for_update(of=("self",)).get(
                subscription_id=subscription.pk,
                slot_id=attendance.enrollment.schedule_id,
            )
        except SubscriptionSlot.DoesNotExist as exc:
            raise SlotBalanceNotFoundError(
                subscription.pk,
                attendance.enrollment.schedule_id,
            ) from exc

        slot.remaining_tokens += 1
        slot.save(update_fields=["remaining_tokens"])
        attendance.token_debited = False
        attendance.save(update_fields=["token_debited"])


def set_attendance_status(
    *, attendance_id: int, status: AttendanceStatus
) -> Attendance:
    # ПОЧЕМУ: статус и движение фишки — одна транзакция, вложенные atomic
    # внутри debit/refund_token схлопываются в savepoint-ы
    with db_transaction.atomic():
        try:
            attendance = Attendance.objects.select_for_update(of=("self",)).get(
                pk=attendance_id
            )
        except Attendance.DoesNotExist as exc:
            raise AttendanceNotFoundError(attendance_id) from exc

        if attendance.status != status:
            was_debited = attendance.token_debited
            attendance.status = status
            attendance.save(update_fields=["status"])

            if status == AttendanceStatus.ATTENDED:
                debit_token(attendance_id)
            elif was_debited:
                # ПОЧЕМУ: бизнес-правило — отмена отметки возвращает фишку
                # (project-context §13.9)
                refund_token(attendance_id)

        attendance.refresh_from_db()
    return attendance
