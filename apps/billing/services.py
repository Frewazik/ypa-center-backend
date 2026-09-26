from __future__ import annotations

import calendar
import logging
import uuid
from contextlib import suppress
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from dataclasses import dataclass
from typing import Literal

from django.db import IntegrityError
from django.db import transaction as db_transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.billing.adapters import (
    GatewayContractError,
    GatewayError,
    PaymentGateway,
    PaymentInfo,
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
    Subscription,
    SubscriptionPlan,
    SubscriptionSlot,
    SubscriptionStatus,
    Transaction,
    TransactionStatus,
)
from apps.billing.ports import SchedulePort, UnknownSlotError
from apps.billing.selectors import active_seat_q
from apps.core.locks import advisory_xact_lock, advisory_xact_lock_many
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
# ПОЧЕМУ: lease обязан переживать сетевой вызов к шлюзу с ретраями,
# но не блокировать возврат надолго после смерти воркера
_REFUND_CLAIM_TTL = timedelta(minutes=10)


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

        try:
            slot = SubscriptionSlot.objects.select_for_update(of=("self",)).get(
                subscription_id=enrollment.subscription_id,
                slot_id=enrollment.schedule_id,
            )
        except SubscriptionSlot.DoesNotExist as exc:
            raise SlotBalanceNotFoundError(
                enrollment.subscription_id, enrollment.schedule_id
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

    # ПОЧЕМУ: защита от IDOR, проверяем принадлежность ребенка плательщику
    if not Student.objects.filter(pk=student_id, parent_id=parent_id).exists():
        raise StudentNotOwnedError(student_id)

    replay, lock_token = _reserve_idempotency(idempotency_key, request_fingerprint)
    if replay is not None:
        return replay

    try:
        with db_transaction.atomic():
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
    if not Student.objects.filter(pk=student_id, parent_id=parent_id).exists():
        raise StudentNotOwnedError(student_id)
    if trial_date < timezone.localdate():
        raise TrialDateUnavailableError(
            schedule_id, trial_date, "дата пробного уже в прошлом."
        )

    replay, lock_token = _reserve_idempotency(idempotency_key, request_fingerprint)
    if replay is not None:
        return replay

    try:
        with db_transaction.atomic():
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
                    schedule_id, trial_date
                )
            except UnknownSlotError as exc:
                raise SlotNotFoundError(schedule_id) from exc
            # ПОЧЕМУ: дата валидна, только если ближайшее занятие «на дату или
            # позже» — ровно эта дата; отмены и переносы масок учтены портом
            if next_lesson != trial_date:
                raise TrialDateUnavailableError(
                    schedule_id,
                    trial_date,
                    "на эту дату занятие группы не проводится.",
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
        start_date, expires_at = _activation_window(slot_ids, schedule_port)
    except UnknownSlotError as exc:
        raise SlotNotFoundError(exc.slot_id) from exc

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

    transaction_id = _verified_transaction_id(info)
    if info.status == "succeeded":
        _apply_success(info, transaction_id, schedule_port)
        return
    if info.status == "canceled":
        _apply_cancellation(transaction_id, payment_id)
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
                and not tx.metadata.get("compensation_required")
            ):
                # ПОЧЕМУ: вебхук опоздал, заказ уже аннулирован (TTL-свипер или
                # аварийное прерывание checkout) — инициируем возврат
                tx.external_id = info.id
                tx.save(update_fields=["external_id"])
                _mark_for_compensation(tx, {"reason": "PAYMENT_SUCCEEDED_AFTER_EXPIRY"})
                deferred = PaymentSucceededAfterExpiryError(info.id, str(tx.pk))
        elif info.currency != _EXPECTED_CURRENCY or info.amount_kopecks != tx.amount:
            tx.status = TransactionStatus.FAILED
            tx.external_id = info.id
            tx.metadata = {
                **tx.metadata,
                "failure_reason": "AMOUNT_MISMATCH",
                "expected_amount_kopecks": tx.amount,
                "expected_currency": _EXPECTED_CURRENCY,
                "gateway_amount_kopecks": info.amount_kopecks,
                "gateway_currency": info.currency,
            }
            tx.save(update_fields=["status", "external_id", "metadata"])
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
                tx.metadata = {
                    **tx.metadata,
                    "failure_reason": "DATA_INTEGRITY",
                    "detail": str(data_error),
                }
                tx.save(update_fields=["status", "external_id", "metadata"])
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
                tx.save(update_fields=["status", "external_id"])

                try:
                    start_date, expires_at = _activation_window(slot_ids, schedule_port)
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
    tx.save(update_fields=["status", "external_id"])

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
    return None


def _mark_for_compensation(tx: Transaction, extra: dict[str, object]) -> None:
    tx.requires_compensation = True
    tx.metadata = {**tx.metadata, "compensation_required": True, **extra}
    tx.save(update_fields=["requires_compensation", "metadata"])


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


def _activation_window(
    slot_ids: list[int], schedule_port: SchedulePort
) -> tuple[date, datetime]:
    # ПОЧЕМУ: согласно checkout-flow.md, абонемент действует строго месяц
    # начиная с даты первого фактического занятия в выбранных слотах
    today = timezone.localdate()
    first_lesson = min(
        schedule_port.get_next_lesson_date(slot_id, today)
        for slot_id in sorted(slot_ids)
    )
    expires_at = timezone.make_aware(
        datetime.combine(_month_after(first_lesson), time(23, 59, 59))
    )
    return first_lesson, expires_at


def sweep_stale_pending_transactions(*, now: datetime | None = None) -> int:
    # ПОЧЕМУ: защита от зависания забронированных слотов, если вебхук
    # от провайдера потерялся или клиент бросил оплату на полпути
    moment = now if now is not None else timezone.now()
    cutoff = moment - _PENDING_TRANSACTION_TTL
    # ПОЧЕМУ: LIMIT без ORDER BY недетерминирован — при переполнении чанка
    # свипер обязан снимать самые старые брони первыми
    stale_ids = list(
        Transaction.objects.filter(
            status=TransactionStatus.PENDING, created_at__lt=cutoff
        )
        .order_by("created_at")
        .values_list("pk", flat=True)[:_SWEEP_CHUNK_SIZE]
    )

    swept = 0
    for tx_id in stale_ids:
        with db_transaction.atomic():
            tx = (
                Transaction.objects.select_for_update()
                .filter(pk=tx_id, status=TransactionStatus.PENDING)
                .first()
            )
            if tx is None:
                continue  # вебхук успел первым — не трогаем
            tx.status = TransactionStatus.CANCELED
            tx.metadata = {**tx.metadata, "canceled_reason": _TTL_EXPIRED_REASON}
            tx.save(update_fields=["status", "metadata"])
            _release_order_resources(tx)
            swept += 1
    return swept


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
            subscription.status = SubscriptionStatus.EXPIRED
            subscription.save(update_fields=["status"])
            Enrollment.objects.filter(
                subscription=subscription,
                status__in=(EnrollmentStatus.HELD, EnrollmentStatus.ENROLLED),
            ).update(status=EnrollmentStatus.CANCELED)
            _credit_unused_sessions(subscription)
            swept += 1
    return swept


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

        try:
            refund = gateway.create_refund(
                payment_id=tx.external_id,
                amount_kopecks=tx.amount,
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
            locked.metadata = {
                **locked.metadata,
                "compensation_required": False,
                "refund_id": refund.id,
                "refund_status": refund.status,
            }
            locked.save(
                update_fields=[
                    "requires_compensation",
                    "compensation_claimed_until",
                    "metadata",
                ]
            )
            issued += 1
    return issued


def _quarantine_refund(tx_id: uuid.UUID, reason: str) -> None:
    with db_transaction.atomic():
        locked = Transaction.objects.select_for_update().get(pk=tx_id)
        if not locked.requires_compensation:
            return
        locked.requires_compensation = False
        locked.compensation_claimed_until = None
        locked.metadata = {
            **locked.metadata,
            "compensation_required": False,
            "refund_status": "failed",
            "refund_error": reason,
        }
        locked.save(
            update_fields=[
                "requires_compensation",
                "compensation_claimed_until",
                "metadata",
            ]
        )


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
