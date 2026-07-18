from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from threading import Barrier, Event, Lock, Thread

import factory
import pytest
from django.db import IntegrityError, connection
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from apps.schedule.tests.factories import ScheduleFactory

from apps.billing import services as billing_services
from apps.billing.adapters import (
    GatewayContractError,
    GatewayError,
    GatewayNetworkError,
    PaymentInfo,
    PaymentNotFoundError,
    RefundInfo,
    _parse_payment_body,
)
from apps.billing.models import (
    Attendance,
    AttendanceStatus,
    DepositEntry,
    DepositEntryReason,
    Enrollment,
    EnrollmentStatus,
    IdempotencyRecord,
    ParentDeposit,
    Subscription,
    SubscriptionPlan,
    SubscriptionSlot,
    SubscriptionStatus,
    Transaction,
    TransactionStatus,
)
from apps.billing.ports import UnknownSlotError
from apps.billing.services import (
    AmountMismatchError,
    CheckoutResult,
    DuplicateEnrollmentError,
    AttendanceNotDebitableError,
    EnrollmentNotEnrolledError,
    IdempotencyKeyReusedError,
    InsufficientTokensError,
    NoAvailableSeatsError,
    PaymentInProgressError,
    PaymentSucceededAfterExpiryError,
    PlanSlotsMismatchError,
    SeatsTakenAfterPaymentError,
    SlotNotFoundError,
    StudentNotOwnedError,
    SubscriptionNotActivatableError,
    SubscriptionNotSpendableError,
    UnlinkedPaymentError,
    _release_reservation,
    confirm_payment,
    create_payment,
    debit_token,
    issue_pending_refunds,
    sweep_expired_subscriptions,
    sweep_finalized_idempotency_records,
    sweep_stale_pending_transactions,
)
from apps.billing.tasks import run_payment_verification
from apps.billing.views import (
    CheckoutSubscriptionView,
    YookassaWebhookView,
    _request_fingerprint,
)
from apps.users.models import Parent, Student

_FP = "test-fingerprint"
_YOOKASSA_IP = "185.71.76.5"


class ParentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "users.Parent"

    full_name = factory.Sequence(lambda n: f"Родитель {n}")
    phone = factory.Sequence(lambda n: f"+7999000{n:04d}")
    email = factory.Sequence(lambda n: f"parent{n}@example.com")


class StudentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "users.Student"

    parent = factory.SubFactory(ParentFactory)
    full_name = factory.Sequence(lambda n: f"Ребёнок {n}")
    dob = date(2015, 1, 1)


class SubscriptionPlanFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = SubscriptionPlan

    name = factory.Sequence(lambda n: f"Тариф {n}")
    slots_count = 2
    price = 700_000
    # ПОЧЕМУ: база для математических расчетов возврата по депозиту (1200 ₽)
    base_session_price = 120_000


class SubscriptionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Subscription

    parent = factory.SubFactory(ParentFactory)
    plan = factory.SubFactory(SubscriptionPlanFactory)
    status = SubscriptionStatus.ACTIVE
    purchase_price = factory.LazyAttribute(lambda o: o.plan.price)
    base_session_price = factory.LazyAttribute(lambda o: o.plan.base_session_price)


class SubscriptionSlotFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = SubscriptionSlot

    subscription = factory.SubFactory(SubscriptionFactory)
    slot_id = factory.Sequence(lambda n: 100 + n)
    remaining_tokens = 4


class EnrollmentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Enrollment

    student = factory.SubFactory(StudentFactory)
    subscription = factory.SubFactory(SubscriptionFactory)
    schedule_id = 100
    status = EnrollmentStatus.ENROLLED


class AttendanceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Attendance

    enrollment = factory.SubFactory(EnrollmentFactory)
    date = date(2026, 7, 6)
    status = AttendanceStatus.ATTENDED
    token_debited = False


class ParentDepositFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ParentDeposit

    parent = factory.SubFactory(ParentFactory)
    balance = 0


@pytest.fixture(autouse=True)
def _seed_schedules(db) -> None:
    # ПОЧЕМУ: удовлетворяет строгие ограничения ForeignKey на уровне БД
    for slot_id in [100, 101, 102, 103, 104, 105, 106, 777]:
        ScheduleFactory(id=slot_id)


@dataclass
class _AuthStub:
    parent: Parent | None
    is_authenticated: bool = True

    @property
    def pk(self) -> int:
        return self.parent.pk if self.parent else 1


@dataclass
class FakeGateway:
    payments: dict[str, PaymentInfo] = field(default_factory=dict)
    refund_calls: list[tuple[str, int, str]] = field(default_factory=list)
    failing_refund_ids: set[str] = field(default_factory=set)

    def get_payment(self, payment_id: str) -> PaymentInfo:
        found = self.payments.get(payment_id)
        if found is None:
            raise PaymentNotFoundError(payment_id)
        return found

    def create_refund(
        self, payment_id: str, amount_kopecks: int, idempotence_key: str
    ) -> RefundInfo:
        if payment_id in self.failing_refund_ids:
            raise GatewayContractError(f"провайдер отверг возврат {payment_id}")
        self.refund_calls.append((payment_id, amount_kopecks, idempotence_key))
        return RefundInfo(id=f"rf-{len(self.refund_calls)}", status="succeeded")


@dataclass
class RaisingGateway:
    error_factory: type[GatewayError] = GatewayNetworkError

    def get_payment(self, payment_id: str) -> PaymentInfo:
        raise self.error_factory("сбой шлюза")

    def create_refund(
        self, payment_id: str, amount_kopecks: int, idempotence_key: str
    ) -> RefundInfo:
        raise self.error_factory("сбой шлюза")


@dataclass
class FakeSchedulePort:
    capacities: dict[int, int] = field(default_factory=dict)
    lesson_dates: dict[int, date] = field(default_factory=dict)
    default_capacity: int = 10
    strict: bool = False  # True → неизвестный слот падает UnknownSlotError

    def get_slot_capacity(self, slot_id: int) -> int:
        if slot_id in self.capacities:
            return self.capacities[slot_id]
        if self.strict:
            raise UnknownSlotError(slot_id)
        return self.default_capacity

    def get_next_lesson_date(self, slot_id: int, on_or_after: date) -> date:
        if slot_id in self.lesson_dates:
            return self.lesson_dates[slot_id]
        if self.strict:
            raise UnknownSlotError(slot_id)
        return on_or_after + timedelta(days=1)


def _port(**capacities: int) -> FakeSchedulePort:
    return FakeSchedulePort(
        capacities={int(k.removeprefix("s")): v for k, v in capacities.items()}
    )


def _checkout(
    slot_ids: list[int],
    *,
    parent: Parent | None = None,
    student: Student | None = None,
    plan: SubscriptionPlan | None = None,
    key: str | None = None,
    fingerprint: str = _FP,
    port: FakeSchedulePort | None = None,
    use_deposit: bool = False,
) -> CheckoutResult:
    the_parent = parent if parent is not None else ParentFactory()
    the_student = student if student is not None else StudentFactory(parent=the_parent)
    the_plan = (
        plan
        if plan is not None
        else SubscriptionPlanFactory(slots_count=len(set(slot_ids)))
    )
    return create_payment(
        the_parent.pk,
        the_plan.pk,
        the_student.pk,
        slot_ids,
        key if key is not None else str(uuid.uuid4()),
        fingerprint,
        schedule_port=port if port is not None else FakeSchedulePort(),
        use_deposit=use_deposit,
    )


def _make_pending_payment(slot_ids: list[int]) -> Transaction:
    # ПОЧЕМУ: в рамках теста могут существовать другие транзакции,
    # извлекаем строго ту, что создана данным вызовом
    before = set(Transaction.objects.values_list("pk", flat=True))
    _checkout(slot_ids)
    return Transaction.objects.exclude(pk__in=before).get()


def _gateway_for(
    tx: Transaction,
    gateway_status: str,
    amount_kopecks: int | None = None,
    currency: str = "RUB",
) -> tuple[str, FakeGateway]:
    payment_id = f"yk-{tx.pk}"
    info = PaymentInfo(
        id=payment_id,
        # ПОЧЕМУ: type: ignore[arg-type] нужен для намеренной симуляции невалидных статусов в тестах
        status=gateway_status,
        transaction_id=str(tx.pk),
        amount_kopecks=tx.amount if amount_kopecks is None else amount_kopecks,
        currency=currency,
    )
    return payment_id, FakeGateway(payments={payment_id: info})


def _make_attendance_with_balance(
    remaining_tokens: int,
    subscription_status: str = SubscriptionStatus.ACTIVE,
    attendance_status: str = AttendanceStatus.ATTENDED,
    enrollment_status: str = EnrollmentStatus.ENROLLED,
) -> Attendance:
    subscription = SubscriptionFactory(status=subscription_status)
    SubscriptionSlotFactory(
        subscription=subscription, slot_id=100, remaining_tokens=remaining_tokens
    )
    enrollment = EnrollmentFactory(
        subscription=subscription, schedule_id=100, status=enrollment_status
    )
    return AttendanceFactory(enrollment=enrollment, status=attendance_status)


@pytest.mark.django_db
class TestDebitToken:
    def test_debit_decrements_balance_and_marks_attendance(self) -> None:
        attendance = _make_attendance_with_balance(remaining_tokens=4)

        debit_token(attendance.pk)

        attendance.refresh_from_db()
        slot = SubscriptionSlot.objects.get()
        assert attendance.token_debited is True
        assert slot.remaining_tokens == 3

    def test_debit_raises_on_zero_balance_and_keeps_it_nonnegative(self) -> None:
        attendance = _make_attendance_with_balance(remaining_tokens=0)

        with pytest.raises(InsufficientTokensError):
            debit_token(attendance.pk)

        attendance.refresh_from_db()
        slot = SubscriptionSlot.objects.get()
        assert slot.remaining_tokens == 0
        assert attendance.token_debited is False

    def test_repeated_debit_is_noop(self) -> None:
        attendance = _make_attendance_with_balance(remaining_tokens=4)

        debit_token(attendance.pk)
        debit_token(attendance.pk)

        slot = SubscriptionSlot.objects.get()
        assert slot.remaining_tokens == 3

    def test_debit_denied_for_non_active_subscription(self) -> None:
        attendance = _make_attendance_with_balance(
            remaining_tokens=4, subscription_status=SubscriptionStatus.EXPIRED
        )

        with pytest.raises(SubscriptionNotSpendableError):
            debit_token(attendance.pk)

        assert SubscriptionSlot.objects.get().remaining_tokens == 4

    def test_debit_denied_for_expired_by_date_subscription(self) -> None:
        attendance = _make_attendance_with_balance(remaining_tokens=4)
        Subscription.objects.update(expires_at=timezone.now() - timedelta(days=1))

        with pytest.raises(SubscriptionNotSpendableError):
            debit_token(attendance.pk)

        assert SubscriptionSlot.objects.get().remaining_tokens == 4

    def test_debit_denied_for_absent_attendance(self) -> None:
        attendance = _make_attendance_with_balance(
            remaining_tokens=4, attendance_status=AttendanceStatus.ABSENT_OK
        )

        with pytest.raises(AttendanceNotDebitableError):
            debit_token(attendance.pk)

        assert SubscriptionSlot.objects.get().remaining_tokens == 4

    def test_debit_denied_for_held_enrollment(self) -> None:
        # ПОЧЕМУ: FSM защищает от списания фишек по неподтвержденной брони (HELD),
        # списание допускается только при статусе ENROLLED
        attendance = _make_attendance_with_balance(
            remaining_tokens=4, enrollment_status=EnrollmentStatus.HELD
        )

        with pytest.raises(EnrollmentNotEnrolledError):
            debit_token(attendance.pk)

        assert SubscriptionSlot.objects.get().remaining_tokens == 4

    def test_db_check_constraint_rejects_negative_balance(self) -> None:
        slot = SubscriptionSlotFactory(remaining_tokens=0)

        with pytest.raises(IntegrityError):
            SubscriptionSlot.objects.filter(pk=slot.pk).update(remaining_tokens=-1)


@pytest.mark.django_db
class TestCreatePayment:
    def test_sequential_repeat_with_same_key_returns_same_url_once(self) -> None:
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        plan = SubscriptionPlanFactory(slots_count=2)
        key = str(uuid.uuid4())
        port = FakeSchedulePort()

        first = create_payment(
            parent.pk, plan.pk, student.pk, [101, 102], key, _FP, schedule_port=port
        )
        second = create_payment(
            parent.pk, plan.pk, student.pk, [101, 102], key, _FP, schedule_port=port
        )

        # ПОЧЕМУ: replay восстанавливает полный контрактный ответ (F9), а не только URL
        assert first == second
        assert first.status == "PENDING_PAYMENT"
        assert first.transaction_id == Transaction.objects.get().pk
        assert first.expires_at is not None
        assert Transaction.objects.count() == 1
        assert Subscription.objects.count() == 1
        assert IdempotencyRecord.objects.count() == 1
        assert Enrollment.objects.count() == 2  # брони не задвоены

    def test_checkout_creates_held_enrollments_for_student(self) -> None:
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        _checkout([101, 102], parent=parent, student=student)

        enrollments = list(Enrollment.objects.order_by("schedule_id"))
        assert [e.schedule_id for e in enrollments] == [101, 102]
        assert all(e.status == EnrollmentStatus.HELD for e in enrollments)
        assert all(e.student_id == student.pk for e in enrollments)
        assert all(not e.is_active for e in enrollments)

    def test_no_seats_rejected_before_payment_and_key_released(self) -> None:
        # ПОЧЕМУ: при нехватке мест брони и заказ откатываются до обращения в кассу
        # ключ идемпотентности освобождается для возможности ретрая
        occupied = EnrollmentFactory(schedule_id=101, status=EnrollmentStatus.ENROLLED)
        assert occupied.occupies_seat
        key = str(uuid.uuid4())

        with pytest.raises(NoAvailableSeatsError):
            _checkout([101], key=key, port=_port(s101=1))

        assert Transaction.objects.count() == 0
        assert Subscription.objects.count() == 1
        assert Enrollment.objects.count() == 1
        assert IdempotencyRecord.objects.count() == 0

    def test_held_seat_blocks_next_buyer(self) -> None:
        # ПОЧЕМУ: бронь (HELD) обязана занимать физическое место в слоте
        # наравне с уже оплаченной записью для защиты от овербукинга
        _checkout([101], port=_port(s101=1))

        with pytest.raises(NoAvailableSeatsError):
            _checkout([101], port=_port(s101=1))

        assert Enrollment.objects.filter(schedule_id=101).count() == 1

    def test_unknown_slot_maps_to_not_found(self) -> None:
        port = FakeSchedulePort(strict=True)

        with pytest.raises(SlotNotFoundError):
            _checkout([777], port=port)

        assert Transaction.objects.count() == 0

    def test_foreign_student_rejected(self) -> None:
        # ПОЧЕМУ: защита от IDOR на уровне домена
        # гарантирует невозможность записать чужого ребенка
        parent = ParentFactory()
        foreign_student = StudentFactory()  # у другого родителя

        with pytest.raises(StudentNotOwnedError):
            _checkout([101], parent=parent, student=foreign_student)

        assert Transaction.objects.count() == 0
        assert Enrollment.objects.count() == 0

    def test_slots_count_mismatch_rejected_before_any_mutation(self) -> None:
        # ПОЧЕМУ: защита от фрода при подмене количества слотов в запросе
        # предотвращает покупку "безлимита" по цене минимального тарифа
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        plan = SubscriptionPlanFactory(slots_count=1, price=400_000)

        with pytest.raises(PlanSlotsMismatchError):
            create_payment(
                parent.pk,
                plan.pk,
                student.pk,
                [101, 102, 103, 104, 105, 106],
                str(uuid.uuid4()),
                _FP,
                schedule_port=FakeSchedulePort(),
            )

        assert Transaction.objects.count() == 0
        assert IdempotencyRecord.objects.count() == 0

    def test_duplicate_slot_ids_rejected_by_domain_not_serializer(self) -> None:
        # ПОЧЕМУ: дублирование слотов в запросе отсекается валидацией домена
        # не допуская падения с IntegrityError на уровне БД
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        plan = SubscriptionPlanFactory(slots_count=2)

        with pytest.raises(PlanSlotsMismatchError):
            create_payment(
                parent.pk,
                plan.pk,
                student.pk,
                [101, 101],
                str(uuid.uuid4()),
                _FP,
                schedule_port=FakeSchedulePort(),
            )

        assert Transaction.objects.count() == 0

    def test_same_key_different_fingerprint_raises_reused(self) -> None:
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        plan = SubscriptionPlanFactory(slots_count=1)
        key = str(uuid.uuid4())
        port = FakeSchedulePort()

        create_payment(
            parent.pk,
            plan.pk,
            student.pk,
            [101],
            key,
            "fingerprint-a",
            schedule_port=port,
        )

        with pytest.raises(IdempotencyKeyReusedError):
            create_payment(
                parent.pk,
                plan.pk,
                student.pk,
                [102],
                key,
                "fingerprint-b",
                schedule_port=port,
            )

        assert Transaction.objects.count() == 1

    def test_live_in_progress_lock_raises_conflict(self) -> None:
        key = str(uuid.uuid4())
        IdempotencyRecord.objects.create(
            key=key,
            request_fingerprint=_FP,
            response_status=202,
            response_body={},
            locked_until=timezone.now() + timedelta(minutes=10),
        )

        with pytest.raises(PaymentInProgressError):
            _checkout([101], key=key)

        assert Transaction.objects.count() == 0

    def test_stale_in_progress_lock_is_reclaimed(self) -> None:
        # !!!: обработка SIGKILL-сценария; если воркер умер и TTL истек
        # лок ключа переотбирается другим процессом и платеж проходит штатно
        key = str(uuid.uuid4())
        IdempotencyRecord.objects.create(
            key=key,
            request_fingerprint=_FP,
            response_status=202,
            response_body={},
            locked_until=timezone.now() - timedelta(seconds=1),
        )

        result = _checkout([101], key=key)

        assert result.payment_url is not None
        assert result.payment_url.startswith("https://yookassa.ru/")
        record = IdempotencyRecord.objects.get(key=key)
        assert record.response_status == 201
        assert record.locked_until is None
        assert record.lock_token is None
        assert Transaction.objects.count() == 1

    def test_business_failure_releases_reservation_and_retry_succeeds(self) -> None:
        # ПОЧЕМУ: мягкий сбой или исключение внутри бизнес-логики
        # обязаны освободить ключ до истечения TTL для быстрого ретрая
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        plan = SubscriptionPlanFactory(slots_count=1)
        key = str(uuid.uuid4())
        port = FakeSchedulePort()

        # Сбой внутри atomic-мутаций (после резервации ключа): порт падает исключением.
        @dataclass
        class ExplodingPort:
            def get_slot_capacity(self, slot_id: int) -> int:
                raise RuntimeError("внезапный сбой интеграции")

        with pytest.raises(RuntimeError):
            create_payment(
                parent.pk,
                plan.pk,
                student.pk,
                [101],
                key,
                _FP,
                schedule_port=ExplodingPort(),
            )

        assert IdempotencyRecord.objects.count() == 0  # резервация освобождена
        assert Transaction.objects.count() == 0

        retry = create_payment(
            parent.pk, plan.pk, student.pk, [101], key, _FP, schedule_port=port
        )
        assert retry.payment_url is not None
        assert retry.payment_url.startswith("https://yookassa.ru/")
        assert Transaction.objects.count() == 1


@pytest.mark.django_db(transaction=True)
class TestCreatePaymentConcurrency:
    def test_parallel_requests_with_same_key_create_single_transaction(self) -> None:
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        plan = SubscriptionPlanFactory(slots_count=1)
        key = str(uuid.uuid4())
        port = FakeSchedulePort()
        workers = 4
        barrier = Barrier(workers)

        def call() -> CheckoutResult | None:
            barrier.wait()
            try:
                return create_payment(
                    parent.pk,
                    plan.pk,
                    student.pk,
                    [101],
                    key,
                    _FP,
                    schedule_port=port,
                )
            except PaymentInProgressError:
                # ПОЧЕМУ: конкурент легитимно попал в окно обработки и получил 409
                return None
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda _: call(), range(workers)))

        urls = {result.payment_url for result in results if result is not None}
        assert len(urls) == 1
        assert Transaction.objects.count() == 1
        assert Subscription.objects.count() == 1
        assert Enrollment.objects.count() == 1
        assert IdempotencyRecord.objects.count() == 1


@pytest.mark.django_db
class TestIdempotencyFencingUnits:
    def test_release_reservation_requires_owner_token(self) -> None:
        # !!!: защита от снятия чужого лока; воркер, очнувшийся после долгой паузы
        # не имеет права удалить резервацию, которую уже перехватил конкурент
        key = str(uuid.uuid4())
        owner_token = uuid.uuid4()
        IdempotencyRecord.objects.create(
            key=key,
            request_fingerprint=_FP,
            response_status=202,
            response_body={},
            locked_until=timezone.now() + timedelta(minutes=10),
            lock_token=owner_token,
        )

        assert _release_reservation(key, uuid.uuid4()) == 0
        assert IdempotencyRecord.objects.filter(key=key).exists()

        assert _release_reservation(key, owner_token) == 1
        assert not IdempotencyRecord.objects.filter(key=key).exists()


@pytest.mark.django_db(transaction=True)
class TestIdempotencyFencingRace:
    def test_resurrected_owner_cannot_double_charge_after_reclaim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # !!!: эмуляция зависания воркера дольше TTL с перехватом лока конкурентом
        # проснувшийся воркер обязан откатить свои мутации и вернуть URL победителя
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        plan = SubscriptionPlanFactory(slots_count=1)
        key = str(uuid.uuid4())
        port = FakeSchedulePort()

        owner_inside = Event()
        gate = Event()
        call_lock = Lock()
        call_counter = {"n": 0}
        real_issue = billing_services._issue_payment_url

        def slow_first_issue(transaction_id: uuid.UUID) -> str:
            with call_lock:
                call_counter["n"] += 1
                is_first = call_counter["n"] == 1
            if is_first:
                owner_inside.set()
                assert gate.wait(timeout=10), "конкурент не отпустил владельца"
            return real_issue(transaction_id)

        monkeypatch.setattr(billing_services, "_issue_payment_url", slow_first_issue)
        # ПОЧЕМУ: TTL=0 заставляет лок владельца мгновенно протухнуть
        # позволяя конкуренту легитимно перехватить обработку
        monkeypatch.setattr(billing_services, "_RESERVATION_TTL", timedelta(0))
        monkeypatch.setattr(
            billing_services, "_lock_slot_for_booking", lambda slot_id: None
        )

        owner_result: dict[str, CheckoutResult] = {}

        def owner_call() -> None:
            try:
                owner_result["url"] = create_payment(
                    parent.pk,
                    plan.pk,
                    student.pk,
                    [101],
                    key,
                    _FP,
                    schedule_port=port,
                )
            finally:
                connection.close()

        owner = Thread(target=owner_call)
        owner.start()
        assert owner_inside.wait(timeout=10), "владелец не дошёл до критической секции"

        competitor_student = StudentFactory(parent=parent)
        competitor = create_payment(
            parent.pk,
            plan.pk,
            competitor_student.pk,
            [101],
            key,
            _FP,
            schedule_port=port,
        )

        gate.set()
        owner.join(timeout=15)
        assert not owner.is_alive()
        # ПОЧЕМУ: оба потока обязаны вернуть результат победителя гонки
        # проигравший процесс восстанавливает данные из сохраненной записи
        assert owner_result["url"].payment_url == competitor.payment_url
        assert owner_result["url"].transaction_id == competitor.transaction_id
        assert Transaction.objects.count() == 1
        assert Subscription.objects.count() == 1
        assert Enrollment.objects.count() == 1
        assert IdempotencyRecord.objects.get(key=key).response_status == 201


@pytest.mark.django_db
class TestConfirmPayment:
    def test_verified_success_enrolls_activates_and_grants_chips(self) -> None:
        tx = _make_pending_payment([101, 102])
        payment_id, gateway = _gateway_for(tx, "succeeded")

        confirm_payment(
            payment_id=payment_id, gateway=gateway, schedule_port=FakeSchedulePort()
        )

        tx.refresh_from_db()
        subscription = Subscription.objects.get()
        assert tx.status == TransactionStatus.SUCCEEDED
        assert tx.external_id == payment_id
        assert subscription.status == SubscriptionStatus.ACTIVE
        assert subscription.expires_at is not None
        assert set(SubscriptionSlot.objects.values_list("slot_id", flat=True)) == {
            101,
            102,
        }
        assert all(s.remaining_tokens == 4 for s in SubscriptionSlot.objects.all())
        assert set(Enrollment.objects.values_list("status", flat=True)) == {
            EnrollmentStatus.ENROLLED
        }

    def test_replay_is_idempotent_noop(self) -> None:
        tx = _make_pending_payment([101, 102])
        payment_id, gateway = _gateway_for(tx, "succeeded")
        port = FakeSchedulePort()

        confirm_payment(payment_id=payment_id, gateway=gateway, schedule_port=port)
        confirm_payment(payment_id=payment_id, gateway=gateway, schedule_port=port)

        assert SubscriptionSlot.objects.count() == 2
        assert Transaction.objects.count() == 1

    def test_forged_webhook_with_unknown_payment_cannot_activate_anything(self) -> None:
        # ПОЧЕМУ: защита от подделки вебхуков, если атакующий угадал UUID транзакции
        # но реального платежа в API провайдера нет, активация блокируется
        tx = _make_pending_payment([101])
        gateway = FakeGateway()  # ЮКасса такого платежа не знает

        with pytest.raises(PaymentNotFoundError):
            confirm_payment(
                payment_id="fake-payment-id",
                gateway=gateway,
                schedule_port=FakeSchedulePort(),
            )

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.PENDING
        assert SubscriptionSlot.objects.count() == 0

    def test_gateway_status_pending_is_noop(self) -> None:
        # !!!: мы не доверяем payload вебхука, если API возвращает pending
        # игнорируем запрос и ждем следующего терминального вебхука
        tx = _make_pending_payment([101])
        payment_id, gateway = _gateway_for(tx, "pending")

        confirm_payment(
            payment_id=payment_id, gateway=gateway, schedule_port=FakeSchedulePort()
        )

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.PENDING
        assert SubscriptionSlot.objects.count() == 0

    def test_verified_cancellation_cancels_order_and_frees_seats(self) -> None:
        tx = _make_pending_payment([101])
        payment_id, gateway = _gateway_for(tx, "canceled")

        confirm_payment(
            payment_id=payment_id, gateway=gateway, schedule_port=FakeSchedulePort()
        )

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.CANCELED
        assert Subscription.objects.get().status == SubscriptionStatus.CANCELED
        assert (
            Enrollment.objects.get().status == EnrollmentStatus.CANCELED
        )  # место возвращено в пул
        assert SubscriptionSlot.objects.count() == 0


@pytest.mark.django_db
class TestConfirmPaymentEnrollmentFSM:
    def test_overbooking_at_webhook_triggers_compensation(self) -> None:
        # ПОЧЕМУ: если за время оплаты на шлюзе физические места закончились
        # транзакция должна уйти в статус требующей компенсации
        port = _port(s101=1)
        tx = _make_pending_payment([101])

        # ПОЧЕМУ: эмулируем перехват слота другим процессом (например, админом) во время оплаты
        EnrollmentFactory(schedule_id=101, status=EnrollmentStatus.ENROLLED)
        payment_id, gateway = _gateway_for(tx, "succeeded")

        with pytest.raises(SeatsTakenAfterPaymentError):
            confirm_payment(payment_id=payment_id, gateway=gateway, schedule_port=port)

        tx.refresh_from_db()
        own = Enrollment.objects.get(subscription_id=tx.subscription_id)
        assert tx.status == TransactionStatus.SUCCEEDED
        assert tx.metadata["compensation_required"] is True
        assert tx.metadata["reason"] == "SEATS_TAKEN_AFTER_PAYMENT"
        assert Subscription.objects.get(pk=tx.subscription_id).status == (
            SubscriptionStatus.CANCELED
        )
        assert own.status == EnrollmentStatus.CANCELED
        assert SubscriptionSlot.objects.count() == 0

    def test_lost_hold_triggers_compensation(self) -> None:
        # ПОЧЕМУ: при аномальной пропаже брони восстановить ее невозможно
        # транзакция обязана уйти в очередь на компенсацию
        tx = _make_pending_payment([101])
        Enrollment.objects.update(status=EnrollmentStatus.CANCELED)
        payment_id, gateway = _gateway_for(tx, "succeeded")

        with pytest.raises(SeatsTakenAfterPaymentError):
            confirm_payment(
                payment_id=payment_id,
                gateway=gateway,
                schedule_port=FakeSchedulePort(),
            )

        tx.refresh_from_db()
        assert tx.metadata["reason"] == "HOLD_LOST"
        assert tx.metadata["compensation_required"] is True

    def test_slot_removed_from_schedule_triggers_compensation(self) -> None:
        tx = _make_pending_payment([101])
        payment_id, gateway = _gateway_for(tx, "succeeded")
        vanished = FakeSchedulePort(strict=True)

        with pytest.raises(SeatsTakenAfterPaymentError):
            confirm_payment(
                payment_id=payment_id, gateway=gateway, schedule_port=vanished
            )

        tx.refresh_from_db()
        assert tx.metadata["reason"] == "SLOT_REMOVED"


@pytest.mark.django_db
class TestConfirmPaymentAmountVerification:
    def test_amount_mismatch_marks_failed_and_frees_resources(self) -> None:
        # ПОЧЕМУ: защита от Partial-Payment fraud (недоплаты),
        # платеж на меньшую сумму не должен активировать тарифа
        tx = _make_pending_payment([101, 102])
        payment_id, gateway = _gateway_for(tx, "succeeded", amount_kopecks=100)

        with pytest.raises(AmountMismatchError):
            confirm_payment(
                payment_id=payment_id, gateway=gateway, schedule_port=FakeSchedulePort()
            )

        tx.refresh_from_db()
        subscription = Subscription.objects.get()
        assert tx.status == TransactionStatus.FAILED
        assert tx.metadata["failure_reason"] == "AMOUNT_MISMATCH"
        assert tx.metadata["expected_amount_kopecks"] == 700_000
        assert tx.metadata["gateway_amount_kopecks"] == 100
        # ПОЧЕМУ: при несовпадении сумм заказ признается неисполнимым
        # заблокированные ресурсы обязаны быть немедленно освобождены
        assert subscription.status == SubscriptionStatus.CANCELED
        assert set(Enrollment.objects.values_list("status", flat=True)) == {
            EnrollmentStatus.CANCELED
        }
        assert SubscriptionSlot.objects.count() == 0

    def test_wrong_currency_marks_failed_even_with_matching_number(self) -> None:
        tx = _make_pending_payment([101])
        payment_id, gateway = _gateway_for(tx, "succeeded", currency="USD")

        with pytest.raises(AmountMismatchError):
            confirm_payment(
                payment_id=payment_id, gateway=gateway, schedule_port=FakeSchedulePort()
            )

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.FAILED
        assert SubscriptionSlot.objects.count() == 0

    def test_failed_verification_is_terminal_replay_is_noop(self) -> None:
        tx = _make_pending_payment([101])
        payment_id, gateway = _gateway_for(tx, "succeeded", amount_kopecks=100)
        port = FakeSchedulePort()

        with pytest.raises(AmountMismatchError):
            confirm_payment(payment_id=payment_id, gateway=gateway, schedule_port=port)
        confirm_payment(payment_id=payment_id, gateway=gateway, schedule_port=port)

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.FAILED
        assert SubscriptionSlot.objects.count() == 0


@pytest.mark.django_db
class TestConfirmPaymentDataIntegrity:
    def test_corrupted_slot_ids_mark_failed_before_any_mutation(self) -> None:
        # ПОЧЕМУ: при поврежденных данных транзакция явно переводится в FAILED
        # для сохранения аудита, тихий откат (rollback) недопустим
        tx = _make_pending_payment([101])
        Transaction.objects.filter(pk=tx.pk).update(selected_slot_ids=["oops"])
        payment_id, gateway = _gateway_for(tx, "succeeded")

        with pytest.raises(UnlinkedPaymentError):
            confirm_payment(
                payment_id=payment_id, gateway=gateway, schedule_port=FakeSchedulePort()
            )

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.FAILED
        assert tx.metadata["failure_reason"] == "DATA_INTEGRITY"
        assert tx.metadata["compensation_required"] is True
        assert Subscription.objects.get().status == SubscriptionStatus.CANCELED
        assert SubscriptionSlot.objects.count() == 0


@pytest.mark.django_db
class TestConfirmPaymentZombieGuard:
    def test_late_success_on_canceled_subscription_flags_compensation(self) -> None:
        # ПОЧЕМУ: защита от зомби-абонементов; опоздавший success
        # не воскрешает отмененный абонемент, деньги идут в очередь на возврат
        tx = _make_pending_payment([101])
        Subscription.objects.update(status=SubscriptionStatus.CANCELED)
        payment_id, gateway = _gateway_for(tx, "succeeded")

        with pytest.raises(SubscriptionNotActivatableError):
            confirm_payment(
                payment_id=payment_id, gateway=gateway, schedule_port=FakeSchedulePort()
            )

        tx.refresh_from_db()
        subscription = Subscription.objects.get()
        assert tx.status == TransactionStatus.SUCCEEDED
        assert tx.metadata["compensation_required"] is True
        assert subscription.status == SubscriptionStatus.CANCELED
        assert subscription.expires_at is None
        assert SubscriptionSlot.objects.count() == 0

        # ПОЧЕМУ: отмененный заказ обязан освободить забронированные места
        assert set(Enrollment.objects.values_list("status", flat=True)) == {
            EnrollmentStatus.CANCELED
        }


@pytest.mark.django_db
class TestSweepers:
    def test_stale_pending_transaction_swept_with_full_release(self) -> None:
        stale = _make_pending_payment([101])
        Transaction.objects.filter(pk=stale.pk).update(
            created_at=timezone.now() - timedelta(hours=1)
        )
        fresh = _make_pending_payment([102])

        swept = sweep_stale_pending_transactions()

        stale.refresh_from_db()
        fresh.refresh_from_db()
        assert swept == 1
        assert stale.status == TransactionStatus.CANCELED
        assert stale.metadata["canceled_reason"] == "TTL_EXPIRED"
        assert (
            Subscription.objects.get(pk=stale.subscription_id).status
            == SubscriptionStatus.CANCELED
        )
        assert (
            Enrollment.objects.get(subscription_id=stale.subscription_id).status
            == EnrollmentStatus.CANCELED
        )
        assert fresh.status == TransactionStatus.PENDING
        assert (
            Enrollment.objects.get(subscription_id=fresh.subscription_id).status
            == EnrollmentStatus.HELD
        )

    def test_sweep_is_chunked_against_oom(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # !!!: защита от OOM Death Loop при миллионных выборках
        # проверяем строгую обработку стейтов чанками
        monkeypatch.setattr(billing_services, "_SWEEP_CHUNK_SIZE", 2)
        for slot in (101, 102, 103):
            _make_pending_payment([slot])
            Transaction.objects.filter(status=TransactionStatus.PENDING).update(
                created_at=timezone.now() - timedelta(hours=1)
            )

        first_tick = sweep_stale_pending_transactions()
        second_tick = sweep_stale_pending_transactions()

        assert first_tick == 2
        assert second_tick == 1
        assert not Transaction.objects.filter(status=TransactionStatus.PENDING).exists()

    def test_expired_active_subscriptions_swept(self) -> None:
        expired_sub = SubscriptionFactory(
            status=SubscriptionStatus.ACTIVE,
            expires_at=timezone.now() - timedelta(days=1),
        )
        alive_sub = SubscriptionFactory(
            status=SubscriptionStatus.ACTIVE,
            expires_at=timezone.now() + timedelta(days=10),
        )

        swept = sweep_expired_subscriptions()

        expired_sub.refresh_from_db()
        alive_sub.refresh_from_db()
        assert swept == 1
        assert expired_sub.status == SubscriptionStatus.EXPIRED
        assert alive_sub.status == SubscriptionStatus.ACTIVE


@pytest.mark.django_db
class TestLateSuccessCompensationFlow:
    # ПОЧЕМУ: контракт §2.2; если платеж прошел успешно уже после срабатывания TTL-свипера
    # восстановить заказ нельзя, средства отправляются на безусловный возврат

    def _swept_then_paid(self) -> tuple[Transaction, str, FakeGateway]:
        tx = _make_pending_payment([101])
        Transaction.objects.filter(pk=tx.pk).update(
            created_at=timezone.now() - timedelta(hours=1)
        )
        assert sweep_stale_pending_transactions() == 1
        payment_id, gateway = _gateway_for(tx, "succeeded")
        return tx, payment_id, gateway

    def test_late_success_flags_compensation_without_activation(self) -> None:
        tx, payment_id, gateway = self._swept_then_paid()

        with pytest.raises(PaymentSucceededAfterExpiryError):
            confirm_payment(
                payment_id=payment_id, gateway=gateway, schedule_port=FakeSchedulePort()
            )

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.CANCELED
        assert tx.external_id == payment_id
        assert tx.metadata["compensation_required"] is True
        assert Subscription.objects.get().status == SubscriptionStatus.CANCELED
        assert SubscriptionSlot.objects.count() == 0

    def test_refund_task_pays_back_once_and_is_idempotent(self) -> None:
        tx, payment_id, gateway = self._swept_then_paid()
        with pytest.raises(PaymentSucceededAfterExpiryError):
            confirm_payment(
                payment_id=payment_id, gateway=gateway, schedule_port=FakeSchedulePort()
            )

        first_run = issue_pending_refunds(gateway=gateway)
        second_run = issue_pending_refunds(gateway=gateway)

        tx.refresh_from_db()
        assert first_run == 1
        assert second_run == 0
        assert gateway.refund_calls == [(payment_id, tx.amount, f"refund-{tx.pk}")]
        assert tx.metadata["compensation_required"] is False
        assert tx.metadata["refund_id"] == "rf-1"
        assert tx.metadata["refund_status"] == "succeeded"

    def test_overbooking_compensation_is_refunded_by_same_pipeline(self) -> None:
        port = _port(s101=1)
        tx = _make_pending_payment([101])
        EnrollmentFactory(schedule_id=101, status=EnrollmentStatus.ENROLLED)
        payment_id, gateway = _gateway_for(tx, "succeeded")
        with pytest.raises(SeatsTakenAfterPaymentError):
            confirm_payment(payment_id=payment_id, gateway=gateway, schedule_port=port)

        issued = issue_pending_refunds(gateway=gateway)

        tx.refresh_from_db()
        assert issued == 1
        assert tx.metadata["compensation_required"] is False
        assert gateway.refund_calls == [(payment_id, tx.amount, f"refund-{tx.pk}")]

    def test_refund_skipped_when_payment_never_confirmed(self) -> None:
        # ПОЧЕМУ: транзакции без external_id не существуют на стороне провайдера
        # инициировать возврат через API в таком случае бессмысленно
        tx = _make_pending_payment([101])
        Transaction.objects.filter(pk=tx.pk).update(
            requires_compensation=True, metadata={"compensation_required": True}
        )
        gateway = FakeGateway()

        assert issue_pending_refunds(gateway=gateway) == 0
        assert gateway.refund_calls == []
        tx.refresh_from_db()
        # ПОЧЕМУ: невыполнимый возврат выводится из очереди в карантин
        # чтобы избежать вечного блокирования refund-воркера
        assert tx.requires_compensation is False
        assert tx.metadata["refund_status"] == "failed"

    def test_active_claim_blocks_parallel_tick_before_network_call(self) -> None:
        # ПОЧЕМУ: claim check — конкурентный тик (дубль крона, ручной запуск)
        # не должен дойти до create_refund, пока lease первого воркера жив
        tx = _make_pending_payment([101])
        Transaction.objects.filter(pk=tx.pk).update(
            external_id=f"yk-{tx.pk}",
            requires_compensation=True,
            metadata={"compensation_required": True},
            compensation_claimed_until=timezone.now() + timedelta(minutes=5),
        )
        gateway = FakeGateway()

        assert issue_pending_refunds(gateway=gateway) == 0
        assert gateway.refund_calls == []
        tx.refresh_from_db()
        assert tx.requires_compensation is True

    def test_expired_claim_is_reclaimed(self) -> None:
        # ПОЧЕМУ: воркер, убитый после сетевого вызова, не хоронит возврат —
        # истёкший lease перехватывается, дубль гасится Idempotence-Key
        tx = _make_pending_payment([101])
        Transaction.objects.filter(pk=tx.pk).update(
            external_id=f"yk-{tx.pk}",
            requires_compensation=True,
            metadata={"compensation_required": True},
            compensation_claimed_until=timezone.now() - timedelta(seconds=1),
        )
        gateway = FakeGateway()

        assert issue_pending_refunds(gateway=gateway) == 1
        assert len(gateway.refund_calls) == 1
        tx.refresh_from_db()
        assert tx.requires_compensation is False
        assert tx.compensation_claimed_until is None
        assert tx.metadata["refund_status"] == "succeeded"

    def test_successful_refund_releases_claim(self) -> None:
        tx = _make_pending_payment([101])
        Transaction.objects.filter(pk=tx.pk).update(
            external_id=f"yk-{tx.pk}",
            requires_compensation=True,
            metadata={"compensation_required": True},
        )
        gateway = FakeGateway()

        assert issue_pending_refunds(gateway=gateway) == 1
        tx.refresh_from_db()
        assert tx.compensation_claimed_until is None

    def test_refunds_are_chunked_against_oom(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(billing_services, "_REFUND_CHUNK_SIZE", 1)
        gateway = FakeGateway()
        for slot in (101, 102):
            tx = _make_pending_payment([slot])
            Transaction.objects.filter(pk=tx.pk).update(
                external_id=f"yk-{tx.pk}",
                requires_compensation=True,
                metadata={"compensation_required": True},
            )

        first_tick = issue_pending_refunds(gateway=gateway)
        second_tick = issue_pending_refunds(gateway=gateway)

        assert first_tick == 1
        assert second_tick == 1
        assert len(gateway.refund_calls) == 2


@pytest.mark.django_db
class TestTaskRetryClassification:
    def test_transient_network_error_propagates_for_retry(self) -> None:
        with pytest.raises(GatewayNetworkError):
            run_payment_verification("yk-1", RaisingGateway(), FakeSchedulePort())

    def test_permanent_contract_error_is_swallowed_as_critical(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # ПОЧЕМУ: при нарушении контракта API ретраи бессмысленны
        # ошибка гасится и логируется как CRITICAL для ручного вмешательства
        run_payment_verification(
            "yk-1",
            RaisingGateway(error_factory=GatewayContractError),
            FakeSchedulePort(),
        )

        assert any(record.levelname == "CRITICAL" for record in caplog.records)

    def test_permanent_not_found_is_swallowed_and_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        run_payment_verification("yk-unknown", FakeGateway(), FakeSchedulePort())

        assert any("поддельный" in message for message in caplog.messages)

    def test_permanent_billing_error_is_swallowed_and_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        tx = _make_pending_payment([101])
        payment_id, gateway = _gateway_for(tx, "succeeded", amount_kopecks=100)

        run_payment_verification(payment_id, gateway, FakeSchedulePort())

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.FAILED
        assert any("ручной разбор" in message for message in caplog.messages)


class TestAdapterAmountParsing:
    def test_parses_rub_amount_to_kopecks(self) -> None:
        body = (
            b'{"id": "yk-1", "status": "succeeded",'
            b' "amount": {"value": "7000.00", "currency": "RUB"},'
            b' "metadata": {"transaction_id": "tid"}}'
        )

        info = _parse_payment_body("yk-1", body)

        assert info.amount_kopecks == 700_000
        assert info.currency == "RUB"

    @pytest.mark.parametrize(
        "amount_json",
        [
            b'{"value": "7000.001", "currency": "RUB"}',
            b'{"value": "-1.00", "currency": "RUB"}',
            b'{"value": "seven", "currency": "RUB"}',
            b'{"value": 7000, "currency": "RUB"}',
            b"null",
        ],
    )
    def test_rejects_malformed_amount(self, amount_json: bytes) -> None:
        body = b'{"id": "yk-1", "status": "succeeded", "amount": ' + amount_json + b"}"

        with pytest.raises(GatewayContractError):
            _parse_payment_body("yk-1", body)


@pytest.mark.django_db
class TestYookassaWebhookView:
    def _post(self, body: dict[str, object], remote_addr: str) -> Response:
        request = APIRequestFactory().post(
            "/api/v1/webhooks/yookassa", body, format="json", REMOTE_ADDR=remote_addr
        )
        return YookassaWebhookView.as_view()(request)

    def test_request_from_unknown_ip_is_rejected(self) -> None:
        body = {"event": "payment.succeeded", "object": {"id": "yk-1"}}

        response = self._post(body, remote_addr="203.0.113.10")

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_valid_request_enqueues_verification_task(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        enqueued: list[str] = []

        async def fake_kiq(payment_id: str) -> None:
            enqueued.append(payment_id)

        monkeypatch.setattr(
            "apps.billing.views.verify_and_process_payment.kiq", fake_kiq
        )
        body = {"event": "payment.succeeded", "object": {"id": "yk-42"}}

        response = self._post(body, remote_addr=_YOOKASSA_IP)

        assert response.status_code == status.HTTP_200_OK
        assert enqueued == ["yk-42"]

    def test_malformed_body_rejected_before_enqueue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        enqueued: list[str] = []

        async def fake_kiq(payment_id: str) -> None:
            enqueued.append(payment_id)

        monkeypatch.setattr(
            "apps.billing.views.verify_and_process_payment.kiq", fake_kiq
        )
        body = {"event": "payment.succeeded", "object": {"id": "../../etc/passwd"}}

        response = self._post(body, remote_addr=_YOOKASSA_IP)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert enqueued == []


@pytest.mark.django_db
class TestCheckoutViewSecurity:
    def _post(
        self,
        user: _AuthStub,
        body: dict[str, int | list[int] | bool],
        key: str | None = None,
    ) -> Response:
        request = APIRequestFactory().post(
            "/api/v1/checkout/subscription",
            body,
            format="json",
            headers={
                "X-Idempotency-Key": key if key is not None else str(uuid.uuid4())
            },
        )
        force_authenticate(request, user=user)
        return CheckoutSubscriptionView.as_view()(request)

    def _stub_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "apps.billing.views.resolve_schedule_port", FakeSchedulePort
        )

    def test_parent_id_in_body_is_ignored_owner_taken_from_auth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_port(monkeypatch)
        owner = ParentFactory()
        student = StudentFactory(parent=owner)
        victim = ParentFactory()
        plan = SubscriptionPlanFactory(slots_count=1)
        user = _AuthStub(parent=owner)

        response = self._post(
            user,
            {
                "plan_id": plan.pk,
                "student_id": student.pk,
                "slot_ids": [101],
                "parent_id": victim.pk,
            },
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert Subscription.objects.get().parent_id == owner.pk
        assert Transaction.objects.get().parent_id == owner.pk

    def test_foreign_student_in_body_gets_403(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_port(monkeypatch)
        owner = ParentFactory()
        foreign_student = StudentFactory()
        plan = SubscriptionPlanFactory(slots_count=1)
        user = _AuthStub(parent=owner)

        response = self._post(
            user,
            {"plan_id": plan.pk, "student_id": foreign_student.pk, "slot_ids": [101]},
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert Transaction.objects.count() == 0
        assert Enrollment.objects.count() == 0

    def test_user_without_parent_profile_gets_403(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_port(monkeypatch)
        plan = SubscriptionPlanFactory(slots_count=1)
        user = _AuthStub(parent=None)

        response = self._post(
            user, {"plan_id": plan.pk, "student_id": 1, "slot_ids": [101]}
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert Subscription.objects.count() == 0

    def test_missing_idempotency_header_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_port(monkeypatch)
        owner = ParentFactory()
        student = StudentFactory(parent=owner)
        plan = SubscriptionPlanFactory(slots_count=1)
        user = _AuthStub(parent=owner)
        request = APIRequestFactory().post(
            "/api/v1/checkout/subscription",
            {"plan_id": plan.pk, "student_id": student.pk, "slot_ids": [101]},
            format="json",
        )
        force_authenticate(request, user=user)

        response = CheckoutSubscriptionView.as_view()(request)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert Transaction.objects.count() == 0


@pytest.mark.django_db
class TestDepositAccrual:
    # ПОЧЕМУ: бизнес-правило возврата; остаток считается по формуле
    # (цена_абонемента) - (посещенные_занятия * базовая_цена_сессии)

    def _expired_subscription(self, remaining_total: int) -> Subscription:
        plan = SubscriptionPlanFactory(
            slots_count=3, price=1_200_000, base_session_price=120_000
        )
        subscription = SubscriptionFactory(
            plan=plan,
            status=SubscriptionStatus.ACTIVE,
            expires_at=timezone.now() - timedelta(days=1),
        )
        per_slot = [4, 4, 4]
        deficit = 12 - remaining_total
        for i in range(3):
            take = min(4, deficit)
            per_slot[i] -= take
            deficit -= take
        for i, remaining in enumerate(per_slot):
            SubscriptionSlotFactory(
                subscription=subscription, slot_id=100 + i, remaining_tokens=remaining
            )
        return subscription

    def test_expiry_credits_unused_money_and_zeroes_tokens(self) -> None:
        subscription = self._expired_subscription(remaining_total=4)  # сходил 8

        swept = sweep_expired_subscriptions()

        subscription.refresh_from_db()
        deposit = ParentDeposit.objects.get(parent_id=subscription.parent_id)
        entry = DepositEntry.objects.get()
        assert swept == 1
        assert subscription.status == SubscriptionStatus.EXPIRED
        assert deposit.balance == 240_000  # 12 000 − 8 × 1 200 = 2 400 ₽
        assert entry.amount == 240_000
        assert entry.reason == DepositEntryReason.SUBSCRIPTION_EXPIRY_CREDIT
        assert entry.subscription_id == subscription.pk
        # «Остаток занятий превращается в 0».
        assert set(
            SubscriptionSlot.objects.values_list("remaining_tokens", flat=True)
        ) == {0}

    def test_second_sweep_does_not_double_credit(self) -> None:
        subscription = self._expired_subscription(remaining_total=4)

        sweep_expired_subscriptions()
        sweep_expired_subscriptions()

        deposit = ParentDeposit.objects.get(parent_id=subscription.parent_id)
        assert deposit.balance == 240_000
        assert DepositEntry.objects.count() == 1

    def test_fully_used_subscription_credits_nothing(self) -> None:
        # ПОЧЕМУ: если стоимость посещенных занятий превышает цену абонемента
        # возврат на депозит обрезается до нуля (отрицательных начислений нет)
        subscription = self._expired_subscription(remaining_total=0)

        sweep_expired_subscriptions()

        assert not ParentDeposit.objects.filter(
            parent_id=subscription.parent_id
        ).exists()
        assert DepositEntry.objects.count() == 0


@pytest.mark.django_db
class TestDepositSpend:
    def test_partial_deposit_reduces_card_amount(self) -> None:
        parent = ParentFactory()
        ParentDepositFactory(parent=parent, balance=240_000)
        plan = SubscriptionPlanFactory(slots_count=1, price=700_000)

        result = _checkout([101], parent=parent, plan=plan, use_deposit=True)

        tx = Transaction.objects.get()
        deposit = ParentDeposit.objects.get(parent=parent)
        entry = DepositEntry.objects.get()
        assert result.payment_url is not None
        assert tx.amount == 460_000
        assert tx.metadata["deposit_applied_kopecks"] == 240_000
        assert deposit.balance == 0
        assert entry.amount == -240_000
        assert entry.reason == DepositEntryReason.CHECKOUT_SPEND
        assert entry.transaction_id == tx.pk

    def test_full_deposit_fulfills_order_without_gateway(self) -> None:
        parent = ParentFactory()
        ParentDepositFactory(parent=parent, balance=800_000)
        plan = SubscriptionPlanFactory(slots_count=1, price=700_000)

        result = _checkout([101], parent=parent, plan=plan, use_deposit=True)

        tx = Transaction.objects.get()
        subscription = Subscription.objects.get()
        assert result.payment_url is None
        assert result.status == "CONFIRMED"
        assert result.expires_at is None
        assert tx.amount == 0
        assert tx.status == TransactionStatus.SUCCEEDED
        assert tx.metadata["paid_from_deposit"] is True
        assert subscription.status == SubscriptionStatus.ACTIVE
        assert subscription.start_date is not None
        assert subscription.expires_at is not None
        assert Enrollment.objects.get().status == EnrollmentStatus.ENROLLED
        assert SubscriptionSlot.objects.get().remaining_tokens == 4
        assert ParentDeposit.objects.get(parent=parent).balance == 100_000

    def test_use_deposit_without_wallet_is_noop(self) -> None:
        parent = ParentFactory()
        plan = SubscriptionPlanFactory(slots_count=1, price=700_000)

        result = _checkout([101], parent=parent, plan=plan, use_deposit=True)

        tx = Transaction.objects.get()
        assert result.payment_url is not None
        assert tx.amount == 700_000
        assert DepositEntry.objects.count() == 0


@pytest.mark.django_db
class TestDepositHoldReturn:
    def _partial_deposit_checkout(self) -> tuple[Parent, Transaction]:
        parent = ParentFactory()
        ParentDepositFactory(parent=parent, balance=240_000)
        plan = SubscriptionPlanFactory(slots_count=1, price=700_000)
        _checkout([101], parent=parent, plan=plan, use_deposit=True)
        return parent, Transaction.objects.get()

    def test_ttl_sweep_returns_deposit_hold(self) -> None:
        parent, tx = self._partial_deposit_checkout()
        Transaction.objects.filter(pk=tx.pk).update(
            created_at=timezone.now() - timedelta(hours=1)
        )

        assert sweep_stale_pending_transactions() == 1

        tx.refresh_from_db()
        deposit = ParentDeposit.objects.get(parent=parent)
        assert deposit.balance == 240_000
        assert tx.metadata["deposit_returned"] is True
        assert DepositEntry.objects.filter(
            reason=DepositEntryReason.ORDER_CANCELED_RETURN, transaction=tx
        ).exists()
        # ПОЧЕМУ: депозитная часть не маршрутизируется через внешние шлюзы
        # поэтому транзакция не попадает в очередь возвратов (requires_compensation=False)
        assert tx.requires_compensation is False

    def test_canceled_webhook_returns_deposit_hold(self) -> None:
        parent, tx = self._partial_deposit_checkout()
        payment_id, gateway = _gateway_for(tx, "canceled")

        confirm_payment(
            payment_id=payment_id, gateway=gateway, schedule_port=FakeSchedulePort()
        )

        tx.refresh_from_db()
        assert ParentDeposit.objects.get(parent=parent).balance == 240_000
        assert tx.metadata["deposit_returned"] is True
        # ПОЧЕМУ: повторное освобождение депозита идемпотентно
        # баланс пользователя математически защищен от задвоения
        sweep_stale_pending_transactions()
        assert ParentDeposit.objects.get(parent=parent).balance == 240_000


@pytest.mark.django_db
class TestRefundQueueDiscipline:
    def _flagged(self, slot: int, created_shift_min: int) -> Transaction:
        tx = _make_pending_payment([slot])
        Transaction.objects.filter(pk=tx.pk).update(
            external_id=f"yk-{tx.pk}",
            requires_compensation=True,
            metadata={"compensation_required": True},
            created_at=timezone.now() - timedelta(minutes=created_shift_min),
        )
        tx.refresh_from_db()
        return tx

    def test_queue_is_fifo_by_created_at(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(billing_services, "_REFUND_CHUNK_SIZE", 1)
        newer = self._flagged(101, created_shift_min=5)
        older = self._flagged(102, created_shift_min=60)
        gateway = FakeGateway()

        issue_pending_refunds(gateway=gateway)

        assert gateway.refund_calls[0][0] == f"yk-{older.pk}"
        assert newer.pk in set(
            Transaction.objects.filter(requires_compensation=True).values_list(
                "pk", flat=True
            )
        )

    def test_poisoned_head_is_quarantined_and_tail_processed(self) -> None:
        """order_by не лечит poison pills — лечит карантин: хвост не блокируется."""
        poisoned = self._flagged(101, created_shift_min=60)
        healthy = self._flagged(102, created_shift_min=5)
        gateway = FakeGateway(failing_refund_ids={f"yk-{poisoned.pk}"})

        issued = issue_pending_refunds(gateway=gateway)

        poisoned.refresh_from_db()
        healthy.refresh_from_db()
        assert issued == 1
        assert poisoned.requires_compensation is False
        assert poisoned.metadata["refund_status"] == "failed"
        assert "отверг" in poisoned.metadata["refund_error"]
        assert healthy.requires_compensation is False
        assert healthy.metadata["refund_id"] == "rf-1"


@pytest.mark.django_db
class TestActivationWindow:
    # ПОЧЕМУ: срок действия абонемента отсчитывается с даты первого занятия
    # согласно checkout-flow.md, а не с момента оплаты

    def test_expiry_counts_from_earliest_lesson_across_slots(self) -> None:
        # ПОЧЕМУ: дата активации вычисляется как минимум по датам всех слотов в корзине
        # независимая сортировка slot_ids гарантирует правильный выбор первой сессии

        port = FakeSchedulePort(
            lesson_dates={101: date(2026, 7, 20), 102: date(2026, 7, 10)}
        )
        tx = _make_pending_payment([101, 102])
        payment_id, gateway = _gateway_for(tx, "succeeded")

        confirm_payment(payment_id=payment_id, gateway=gateway, schedule_port=port)

        subscription = Subscription.objects.get()
        assert subscription.start_date == date(2026, 7, 10)
        local_expiry = timezone.localtime(subscription.expires_at)
        assert local_expiry.date() == date(2026, 8, 10)
        assert local_expiry.time() == time(23, 59, 59)

    def test_prepaid_order_window_comes_from_port_too(self) -> None:
        parent = ParentFactory()
        ParentDepositFactory(parent=parent, balance=800_000)
        plan = SubscriptionPlanFactory(slots_count=1, price=700_000)
        port = FakeSchedulePort(lesson_dates={101: date(2026, 7, 15)})

        result = _checkout([101], parent=parent, plan=plan, use_deposit=True, port=port)

        subscription = Subscription.objects.get()
        assert result.payment_url is None
        assert subscription.start_date == date(2026, 7, 15)
        assert timezone.localtime(subscription.expires_at).date() == date(2026, 8, 15)


@pytest.mark.django_db
class TestPriceSnapshotImmunity:
    def test_credit_uses_purchase_snapshot_not_live_plan(self) -> None:
        # ПОЧЕМУ: возврат рассчитывается строго по зафиксированному при покупке снапшоту цен
        # изменение тарифа в будущем не позволяет клиенту заработать на разнице
        parent = ParentFactory()
        plan = SubscriptionPlanFactory(
            slots_count=1, price=700_000, base_session_price=120_000
        )
        _checkout([101], parent=parent, plan=plan)
        tx = Transaction.objects.get()
        payment_id, gateway = _gateway_for(tx, "succeeded")
        confirm_payment(
            payment_id=payment_id, gateway=gateway, schedule_port=FakeSchedulePort()
        )
        SubscriptionPlan.objects.filter(pk=plan.pk).update(
            price=1_200_000, base_session_price=999_999, slots_count=5
        )
        Subscription.objects.update(expires_at=timezone.now() - timedelta(days=1))

        assert sweep_expired_subscriptions() == 1

        deposit = ParentDeposit.objects.get(parent=parent)
        assert deposit.balance == 700_000
        assert DepositEntry.objects.get().amount == 700_000


@pytest.mark.django_db
class TestSeatNecromancy:
    def test_expired_subscription_frees_seat_for_next_buyer(self) -> None:
        # ПОЧЕМУ: пул мест обновляется ежемесячно
        # истекший абонемент обязан освободить место для следующих покупателей
        port = _port(s101=1)
        _checkout([101], port=port)
        tx = Transaction.objects.get()
        payment_id, gateway = _gateway_for(tx, "succeeded")
        confirm_payment(payment_id=payment_id, gateway=gateway, schedule_port=port)
        dead = Enrollment.objects.get()
        assert (
            dead.status == EnrollmentStatus.ENROLLED
        )  # место занято мертвецом-будущим
        Subscription.objects.update(expires_at=timezone.now() - timedelta(days=1))

        assert sweep_expired_subscriptions() == 1

        dead.refresh_from_db()
        assert dead.status == EnrollmentStatus.CANCELED
        result = _checkout([101], port=_port(s101=1))
        assert result.payment_url is not None
        assert Enrollment.objects.filter(status=EnrollmentStatus.HELD).count() == 1


@pytest.mark.django_db(transaction=True)
class TestExpiryCreditRace:
    def test_expiry_credit_waits_for_inflight_debit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # !!!: CWE-362 (Race Condition)
        # sweeper обязан ждать снятия FOR UPDATE лока от debit_token
        # чтобы не рассчитать возврат депозита по старым данным MVCC-снапшота
        plan = SubscriptionPlanFactory(
            slots_count=1, price=700_000, base_session_price=120_000
        )
        subscription = SubscriptionFactory(
            plan=plan,
            status=SubscriptionStatus.ACTIVE,
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        SubscriptionSlotFactory(
            subscription=subscription, slot_id=100, granted_tokens=4, remaining_tokens=4
        )
        enrollment = EnrollmentFactory(subscription=subscription, schedule_id=100)
        attendance = AttendanceFactory(enrollment=enrollment)

        debit_holding_lock = Event()
        release_debit = Event()
        original_save = SubscriptionSlot.save

        def stalling_save(
            self: SubscriptionSlot, *args: object, **kwargs: object
        ) -> None:
            original_save(self, *args, **kwargs)
            debit_holding_lock.set()
            assert release_debit.wait(timeout=15), "свипер не отпустил списание"

        monkeypatch.setattr(SubscriptionSlot, "save", stalling_save)

        sweeper_claimed = Event()
        original_sub_save = Subscription.save

        def signalling_sub_save(
            self: Subscription, *args: object, **kwargs: object
        ) -> None:
            original_sub_save(self, *args, **kwargs)
            if self.status == SubscriptionStatus.EXPIRED:
                sweeper_claimed.set()

        monkeypatch.setattr(Subscription, "save", signalling_sub_save)

        def run_debit() -> None:
            try:
                debit_token(attendance.pk)
            finally:
                connection.close()

        def run_sweeper() -> None:
            try:
                sweep_expired_subscriptions()
            finally:
                connection.close()

        debit_worker = Thread(target=run_debit)
        debit_worker.start()
        assert debit_holding_lock.wait(timeout=15), "debit не взял лок слота"
        Subscription.objects.filter(pk=subscription.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        sweeper = Thread(target=run_sweeper)
        sweeper.start()
        # !!!: отпускаем блокировку списания только после вызова claim у свипера
        # тест проверяет, что свипер блокируется и ждет FOR UPDATE лока
        # гарантируя чтение закоммиченного остатка (3 фишки, а не докоммитных 4)
        assert sweeper_claimed.wait(timeout=15), "свипер не дошёл до claim"
        release_debit.set()
        debit_worker.join(timeout=15)
        sweeper.join(timeout=15)
        assert not debit_worker.is_alive() and not sweeper.is_alive()

        attendance.refresh_from_db()
        deposit = ParentDeposit.objects.get(parent=subscription.parent)
        assert attendance.token_debited is True
        assert deposit.balance == 580_000
        assert SubscriptionSlot.objects.get().remaining_tokens == 0


@pytest.mark.django_db
class TestDuplicateEnrollmentGuard:
    def test_double_checkout_same_student_slot_rejected(self) -> None:
        # ПОЧЕМУ: инвариант схемы данных защищает от проблемы двойного клика
        # отправка разных ключей идемпотентности не позволит занять два места
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        plan = SubscriptionPlanFactory(slots_count=1)
        _checkout([101], parent=parent, student=student, plan=plan)

        with pytest.raises(DuplicateEnrollmentError):
            _checkout([101], parent=parent, student=student, plan=plan)

        assert Transaction.objects.count() == 1
        assert Enrollment.objects.count() == 1
        assert IdempotencyRecord.objects.count() == 1

    def test_rebuy_after_cancellation_allowed(self) -> None:
        # ПОЧЕМУ: partial-индекс исключает отмененные записи (CANCELED)
        # позволяя пользователю повторно купить слот после отмены
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        plan = SubscriptionPlanFactory(slots_count=1)
        _checkout([101], parent=parent, student=student, plan=plan)
        Transaction.objects.update(created_at=timezone.now() - timedelta(hours=1))
        assert sweep_stale_pending_transactions() == 1  # бронь → CANCELED

        result = _checkout([101], parent=parent, student=student, plan=plan)

        assert result.payment_url is not None
        assert Enrollment.objects.filter(status=EnrollmentStatus.HELD).count() == 1
        assert Enrollment.objects.filter(status=EnrollmentStatus.CANCELED).count() == 1


@pytest.mark.django_db
class TestHoldLiveness:
    def test_stale_hold_does_not_block_seat_before_sweeper_tick(self) -> None:
        # ПОЧЕМУ: формула занятости игнорирует истекшие HELD-брони
        # мертвая бронь не заблокирует группу в ожидании тика свипера
        _checkout([101], port=_port(s101=1))
        Enrollment.objects.update(created_at=timezone.now() - timedelta(hours=1))

        result = _checkout([101], port=_port(s101=1))

        assert result.payment_url is not None
        assert Enrollment.objects.filter(status=EnrollmentStatus.HELD).count() == 1
        assert Enrollment.objects.filter(status=EnrollmentStatus.CANCELED).count() == 1

    def test_same_student_retry_after_abandoned_checkout_succeeds(self) -> None:
        # ПОЧЕМУ: мертвая бронь (HELD) отменяется критическим путем
        # если клиент забросил оплату и вернулся до срабатывания свипера
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        plan = SubscriptionPlanFactory(slots_count=1)
        _checkout([101], parent=parent, student=student, plan=plan)
        Enrollment.objects.update(created_at=timezone.now() - timedelta(minutes=20))

        result = _checkout([101], parent=parent, student=student, plan=plan)

        assert result.payment_url is not None
        assert Transaction.objects.count() == 2
        assert Enrollment.objects.filter(status=EnrollmentStatus.HELD).count() == 1
        assert Enrollment.objects.filter(status=EnrollmentStatus.CANCELED).count() == 1


@pytest.mark.django_db
class TestIdempotencyGarbageCollection:
    def test_old_records_purged_fresh_and_alive_kept(self) -> None:
        stale_final = IdempotencyRecord.objects.create(
            key=str(uuid.uuid4()),
            request_fingerprint=_FP,
            response_status=201,
            response_body={},
            locked_until=None,
        )
        stale_stuck = IdempotencyRecord.objects.create(
            key=str(uuid.uuid4()),
            request_fingerprint=_FP,
            response_status=202,
            response_body={},
            locked_until=timezone.now() - timedelta(hours=30),
        )
        fresh = IdempotencyRecord.objects.create(
            key=str(uuid.uuid4()),
            request_fingerprint=_FP,
            response_status=201,
            response_body={},
            locked_until=None,
        )
        IdempotencyRecord.objects.filter(
            pk__in=(stale_final.pk, stale_stuck.pk)
        ).update(created_at=timezone.now() - timedelta(hours=30))

        purged = sweep_finalized_idempotency_records()

        assert purged == 2
        remaining = set(IdempotencyRecord.objects.values_list("key", flat=True))
        assert remaining == {fresh.key}


class TestFingerprintScoping:
    def test_fingerprint_is_scoped_by_parent(self) -> None:
        # ПОЧЕМУ: кросс-тенантная коллизия ключа идемпотентности невозможна
        # так как fingerprint аппаратно изолирован по аккаунту родителя
        body: dict[str, object] = {
            "plan_id": 1,
            "student_id": 5,
            "slot_ids": [101],
            "use_deposit": False,
        }

        fp_one = _request_fingerprint("/api/v1/checkout/subscription", body, 1)
        fp_two = _request_fingerprint("/api/v1/checkout/subscription", body, 2)

        assert fp_one != fp_two


@pytest.mark.django_db
class TestWebhookEventFiltering:
    def test_non_payment_event_gets_200_without_enqueue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ПОЧЕМУ: неплатежные вебхуки возвращают 200 OK без логирования
        # чтобы не засорять канал алертов ложными срабатываниями
        enqueued: list[str] = []

        async def fake_kiq(payment_id: str) -> None:
            enqueued.append(payment_id)

        monkeypatch.setattr(
            "apps.billing.views.verify_and_process_payment.kiq", fake_kiq
        )
        body = {"event": "refund.succeeded", "object": {"id": "rf-777"}}
        request = APIRequestFactory().post(
            "/api/v1/webhooks/yookassa", body, format="json", REMOTE_ADDR=_YOOKASSA_IP
        )

        response = YookassaWebhookView.as_view()(request)

        assert response.status_code == status.HTTP_200_OK
        assert response.data == {"status": "ignored"}
        assert enqueued == []

    def test_non_payment_event_with_alien_object_id_still_200(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ПОЧЕМУ: неизвестные события должны тихо игнорироваться
        # возврат 4xx на невалидный формат спровоцирует ретрай-шторм от провайдера
        enqueued: list[str] = []

        async def fake_kiq(payment_id: str) -> None:
            enqueued.append(payment_id)

        monkeypatch.setattr(
            "apps.billing.views.verify_and_process_payment.kiq", fake_kiq
        )
        body = {"event": "refund.succeeded", "object": {"id": "../../etc/passwd"}}
        request = APIRequestFactory().post(
            "/api/v1/webhooks/yookassa", body, format="json", REMOTE_ADDR=_YOOKASSA_IP
        )

        response = YookassaWebhookView.as_view()(request)

        assert response.status_code == status.HTTP_200_OK
        assert response.data == {"status": "ignored"}
        assert enqueued == []


@pytest.mark.django_db
class TestGrantedTokensSnapshot:
    def test_credit_formula_uses_granted_snapshot_of_each_slot(self) -> None:
        # ПОЧЕМУ: математика возврата опирается на замороженный снапшот выданных фишек
        # а не на дефолтное значение модели
        plan = SubscriptionPlanFactory(
            slots_count=1, price=700_000, base_session_price=120_000
        )
        subscription = SubscriptionFactory(
            plan=plan,
            status=SubscriptionStatus.ACTIVE,
            expires_at=timezone.now() - timedelta(days=1),
        )
        SubscriptionSlotFactory(
            subscription=subscription, slot_id=100, granted_tokens=4, remaining_tokens=1
        )

        assert sweep_expired_subscriptions() == 1

        deposit = ParentDeposit.objects.get(parent=subscription.parent)
        assert deposit.balance == 340_000


@pytest.mark.django_db
class TestPaymentInProgressEnvelope:
    def test_conflict_is_problem_json_with_retry_after(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ПОЧЕМУ: формируемый вручную Response обязан соблюдать стандарт RFC 9457
        # чтобы клиентская логика парсинга не ломалась на кастомных ошибках
        monkeypatch.setattr(
            "apps.billing.views.resolve_schedule_port", FakeSchedulePort
        )
        owner = ParentFactory()
        student = StudentFactory(parent=owner)
        plan = SubscriptionPlanFactory(slots_count=1)
        body: dict[str, int | list[int] | bool] = {
            "plan_id": plan.pk,
            "student_id": student.pk,
            "slot_ids": [101],
            "use_deposit": False,
        }
        key = str(uuid.uuid4())
        IdempotencyRecord.objects.create(
            key=key,
            request_fingerprint=_request_fingerprint(
                "/api/v1/checkout/subscription", body, owner.pk
            ),
            response_status=202,
            response_body={},
            locked_until=timezone.now() + timedelta(minutes=10),
        )
        request = APIRequestFactory().post(
            "/api/v1/checkout/subscription",
            body,
            format="json",
            headers={"X-Idempotency-Key": key},
        )
        force_authenticate(request, user=_AuthStub(parent=owner))

        response = CheckoutSubscriptionView.as_view()(request)
        response.render()

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response["Retry-After"] == "5"
        assert response["Content-Type"].startswith("application/problem+json")
        assert response.data["code"] == "PAYMENT_IN_PROGRESS"
        assert response.data["status"] == 409
        assert response.data["type"].endswith("/payment-in-progress")
        assert response.data["instance"] == "/api/v1/checkout/subscription"
        assert Transaction.objects.count() == 0


@pytest.mark.django_db(transaction=True)
class TestEvictionReleaseDeadlock:
    def test_retry_checkout_does_not_deadlock_with_ttl_sweeper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # !!!: предотвращение ABBA-дедлока между процессом checkout и sweeper-ом
        # проверяем единый порядок захвата блокировок: Enrollment -> Deposit
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        ParentDepositFactory(parent=parent, balance=240_000)
        plan = SubscriptionPlanFactory(slots_count=1, price=700_000)
        _checkout([101], parent=parent, student=student, plan=plan, use_deposit=True)
        abandoned_tx = Transaction.objects.get()
        Transaction.objects.update(created_at=timezone.now() - timedelta(hours=1))
        Enrollment.objects.update(created_at=timezone.now() - timedelta(hours=1))

        checkout_evicted = Event()
        sweeper_holds_deposit = Event()
        errors: list[Exception] = []

        original_occupied = billing_services._occupied_seats

        def pausing_occupied(
            slot_id: int, *, exclude_enrollment_pk: int | None = None
        ) -> int:
            # ПОЧЕМУ: эмулируем задержку после захвата лока на Enrollment
            # для проверки поведения ожидающего sweeper-а
            checkout_evicted.set()
            sweeper_holds_deposit.wait(timeout=2)
            return original_occupied(
                slot_id, exclude_enrollment_pk=exclude_enrollment_pk
            )

        original_return_hold = billing_services._return_deposit_hold

        def signalling_return_hold(tx: Transaction) -> None:
            original_return_hold(tx)
            sweeper_holds_deposit.set()
            assert checkout_evicted.wait(timeout=15)

        monkeypatch.setattr(billing_services, "_occupied_seats", pausing_occupied)
        monkeypatch.setattr(
            billing_services, "_return_deposit_hold", signalling_return_hold
        )

        def run_checkout() -> None:
            try:
                _checkout(
                    [101],
                    parent=parent,
                    student=student,
                    plan=plan,
                    use_deposit=True,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connection.close()

        def run_sweeper() -> None:
            try:
                sweep_stale_pending_transactions()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connection.close()

        checkout_worker = Thread(target=run_checkout)
        sweeper_worker = Thread(target=run_sweeper)
        checkout_worker.start()
        assert checkout_evicted.wait(timeout=15), "чекаут не дошёл до эвакуации"
        sweeper_worker.start()
        checkout_worker.join(timeout=20)
        sweeper_worker.join(timeout=20)

        assert not checkout_worker.is_alive() and not sweeper_worker.is_alive()
        assert errors == []

        abandoned_tx.refresh_from_db()
        retry_tx = Transaction.objects.exclude(pk=abandoned_tx.pk).get()
        deposit = ParentDeposit.objects.get(parent=parent)
        assert abandoned_tx.status == TransactionStatus.CANCELED
        assert abandoned_tx.metadata["deposit_returned"] is True
        assert deposit.balance == 240_000
        assert retry_tx.status == TransactionStatus.PENDING
        assert retry_tx.amount == 700_000
        assert "deposit_applied_kopecks" not in retry_tx.metadata
        assert Enrollment.objects.filter(status=EnrollmentStatus.HELD).count() == 1
        assert Enrollment.objects.filter(status=EnrollmentStatus.CANCELED).count() == 1
