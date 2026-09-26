from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.billing.models import (
    Attendance,
    AttendanceStatus,
    Enrollment,
    EnrollmentStatus,
    EnrollmentType,
    SubscriptionStatus,
    Transaction,
    TransactionStatus,
)
from apps.billing.services import (
    CheckoutResult,
    DuplicateEnrollmentError,
    NoAvailableSeatsError,
    SeatsTakenAfterPaymentError,
    StudentNotOwnedError,
    TrialDateUnavailableError,
    TrialLimitExceededError,
    _occupied_seats,
    _occupied_seats_bulk,
    confirm_payment,
    create_payment,
    create_trial_payment,
    sweep_stale_pending_transactions,
)
from apps.billing.tests.test_billing import (
    EnrollmentFactory,
    FakeGateway,
    FakeSchedulePort,
    ParentFactory,
    StudentFactory,
    SubscriptionPlanFactory,
    _checkout,
    _gateway_for,
)
from apps.billing.views import CheckoutTrialView
from apps.journal.services import open_lesson
from apps.schedule.models import Schedule
from apps.users.models import Parent, Student

if TYPE_CHECKING:
    from pytest_django import DjangoAssertNumQueries

_FP = "trial-fingerprint"


def _trial_port(
    slot_id: int,
    trial_date,  # noqa: ANN001 — datetime.date, тесты вне mypy-скоупа
    *,
    capacity: int = 10,
    price: int | None = None,
) -> FakeSchedulePort:
    port = FakeSchedulePort(capacities={slot_id: capacity})
    port.lesson_dates[slot_id] = trial_date
    if price is not None:
        port.default_trial_price = price
    return port


def _trial_checkout(
    slot_id: int,
    *,
    parent: Parent | None = None,
    student: Student | None = None,
    days_ahead: int = 3,
    price: int | None = None,
    capacity: int = 10,
    key: str | None = None,
    gateway: FakeGateway | None = None,
) -> tuple[CheckoutResult, Parent, Student]:
    the_parent = parent if parent is not None else ParentFactory()
    the_student = student if student is not None else StudentFactory(parent=the_parent)
    trial_date = timezone.localdate() + timedelta(days=days_ahead)
    result = create_trial_payment(
        parent_id=the_parent.pk,
        student_id=the_student.pk,
        schedule_id=slot_id,
        trial_date=trial_date,
        idempotency_key=key if key is not None else str(uuid.uuid4()),
        request_fingerprint=_FP,
        gateway=gateway if gateway is not None else FakeGateway(),
        schedule_port=_trial_port(slot_id, trial_date, capacity=capacity, price=price),
    )
    return result, the_parent, the_student


@pytest.mark.django_db
class TestCreateTrialPayment:
    def test_paid_trial_creates_held_enrollment_and_pending_tx(self) -> None:
        gateway = FakeGateway()
        result, parent, student = _trial_checkout(101, gateway=gateway)

        assert result.status == "PENDING_PAYMENT"
        assert result.payment_url is not None
        enrollment = Enrollment.objects.get(type=EnrollmentType.TRIAL)
        assert enrollment.status == EnrollmentStatus.HELD
        assert enrollment.subscription_id is None
        assert enrollment.student_id == student.pk
        assert enrollment.activity_id == Schedule.objects.get(pk=101).activity_id
        tx = Transaction.objects.get()
        assert tx.status == TransactionStatus.PENDING
        assert tx.enrollment_id == enrollment.pk
        assert tx.amount == 120_000
        assert tx.external_id is not None
        assert len(gateway.created_payments) == 1
        # бронь занимает место до подтверждения оплаты
        assert _occupied_seats(101) == 1

    def test_free_trial_is_confirmed_without_gateway(self) -> None:
        gateway = FakeGateway()
        result, _, _ = _trial_checkout(101, price=0, gateway=gateway)

        assert result.status == "CONFIRMED"
        assert result.payment_url is None
        assert gateway.created_payments == []
        enrollment = Enrollment.objects.get(type=EnrollmentType.TRIAL)
        assert enrollment.status == EnrollmentStatus.ENROLLED
        assert Transaction.objects.get().status == TransactionStatus.SUCCEEDED

    def test_second_trial_same_activity_hits_limit(self) -> None:
        result, parent, student = _trial_checkout(101)
        # ПОЧЕМУ другой слот: лимит действует на уровне кружка, а не слота —
        # слот 102 получает кружок слота 101 через trial_infos
        trial_date = timezone.localdate() + timedelta(days=4)
        port = _trial_port(102, trial_date)
        port.trial_infos[102] = port.get_slot_trial_info(101)

        with pytest.raises(TrialLimitExceededError):
            create_trial_payment(
                parent_id=parent.pk,
                student_id=student.pk,
                schedule_id=102,
                trial_date=trial_date,
                idempotency_key=str(uuid.uuid4()),
                request_fingerprint=_FP,
                gateway=FakeGateway(),
                schedule_port=port,
            )

    def test_canceled_trial_frees_the_limit(self) -> None:
        result, parent, student = _trial_checkout(101)
        Enrollment.objects.filter(type=EnrollmentType.TRIAL).update(
            status=EnrollmentStatus.CANCELED
        )

        retry, _, _ = _trial_checkout(101, parent=parent, student=student)

        assert retry.status == "PENDING_PAYMENT"
        assert (
            Enrollment.objects.filter(
                type=EnrollmentType.TRIAL, status=EnrollmentStatus.HELD
            ).count()
            == 1
        )

    def test_date_without_lesson_is_rejected(self) -> None:
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        trial_date = timezone.localdate() + timedelta(days=3)
        port = FakeSchedulePort(capacities={101: 10})
        # lesson_dates не задан: порт вернёт trial_date + 1 день → несовпадение

        with pytest.raises(TrialDateUnavailableError):
            create_trial_payment(
                parent_id=parent.pk,
                student_id=student.pk,
                schedule_id=101,
                trial_date=trial_date,
                idempotency_key=str(uuid.uuid4()),
                request_fingerprint=_FP,
                gateway=FakeGateway(),
                schedule_port=port,
            )
        assert Enrollment.objects.count() == 0

    def test_past_date_is_rejected(self) -> None:
        parent = ParentFactory()
        student = StudentFactory(parent=parent)

        with pytest.raises(TrialDateUnavailableError):
            create_trial_payment(
                parent_id=parent.pk,
                student_id=student.pk,
                schedule_id=101,
                trial_date=timezone.localdate() - timedelta(days=1),
                idempotency_key=str(uuid.uuid4()),
                request_fingerprint=_FP,
                gateway=FakeGateway(),
                schedule_port=FakeSchedulePort(),
            )

    def test_foreign_student_is_rejected(self) -> None:
        parent = ParentFactory()
        foreign_student = StudentFactory()

        with pytest.raises(StudentNotOwnedError):
            create_trial_payment(
                parent_id=parent.pk,
                student_id=foreign_student.pk,
                schedule_id=101,
                trial_date=timezone.localdate() + timedelta(days=3),
                idempotency_key=str(uuid.uuid4()),
                request_fingerprint=_FP,
                gateway=FakeGateway(),
                schedule_port=FakeSchedulePort(),
            )

    def test_full_slot_is_rejected(self) -> None:
        _trial_checkout(101, capacity=1)

        with pytest.raises(NoAvailableSeatsError):
            _trial_checkout(101, capacity=1)

    def test_replay_with_same_key_returns_same_result(self) -> None:
        key = str(uuid.uuid4())
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        first, _, _ = _trial_checkout(101, parent=parent, student=student, key=key)
        second, _, _ = _trial_checkout(101, parent=parent, student=student, key=key)

        assert first == second
        assert Transaction.objects.count() == 1
        assert Enrollment.objects.count() == 1


@pytest.mark.django_db
class TestTrialSeatLifecycle:
    def test_past_trial_frees_the_seat(self) -> None:
        result, _, _ = _trial_checkout(101, price=0)
        assert _occupied_seats(101) == 1

        Enrollment.objects.filter(type=EnrollmentType.TRIAL).update(
            trial_date=timezone.localdate() - timedelta(days=1)
        )

        assert _occupied_seats(101) == 0

    def test_sweeper_releases_stale_trial_hold(self) -> None:
        _trial_checkout(101)
        Transaction.objects.update(created_at=timezone.now() - timedelta(hours=1))
        Enrollment.objects.update(created_at=timezone.now() - timedelta(hours=1))

        assert sweep_stale_pending_transactions() == 1

        enrollment = Enrollment.objects.get(type=EnrollmentType.TRIAL)
        assert enrollment.status == EnrollmentStatus.CANCELED
        assert _occupied_seats(101) == 0


@pytest.mark.django_db
class TestTrialWebhookConfirmation:
    def test_verified_success_enrolls_trial(self) -> None:
        trial_date = timezone.localdate() + timedelta(days=3)
        result, _, _ = _trial_checkout(101)
        tx = Transaction.objects.get()
        payment_id, gateway = _gateway_for(tx, "succeeded")

        confirm_payment(
            payment_id=payment_id,
            gateway=gateway,
            schedule_port=_trial_port(101, trial_date),
        )

        tx.refresh_from_db()
        enrollment = Enrollment.objects.get(type=EnrollmentType.TRIAL)
        assert tx.status == TransactionStatus.SUCCEEDED
        assert enrollment.status == EnrollmentStatus.ENROLLED

    def test_cancellation_releases_trial_hold(self) -> None:
        result, _, _ = _trial_checkout(101)
        tx = Transaction.objects.get()
        payment_id, gateway = _gateway_for(tx, "canceled")

        confirm_payment(
            payment_id=payment_id,
            gateway=gateway,
            schedule_port=FakeSchedulePort(),
        )

        tx.refresh_from_db()
        enrollment = Enrollment.objects.get(type=EnrollmentType.TRIAL)
        assert tx.status == TransactionStatus.CANCELED
        assert enrollment.status == EnrollmentStatus.CANCELED

    def test_hold_lost_before_webhook_marks_compensation(self) -> None:
        result, _, _ = _trial_checkout(101)
        tx = Transaction.objects.get()
        Enrollment.objects.filter(pk=tx.enrollment_id).update(
            status=EnrollmentStatus.CANCELED
        )
        payment_id, gateway = _gateway_for(tx, "succeeded")

        with pytest.raises(SeatsTakenAfterPaymentError):
            confirm_payment(
                payment_id=payment_id,
                gateway=gateway,
                schedule_port=FakeSchedulePort(),
            )

        tx.refresh_from_db()
        assert tx.status == TransactionStatus.SUCCEEDED
        assert tx.requires_compensation is True

    def test_late_success_after_sweep_goes_to_refund(self) -> None:
        _trial_checkout(101)
        tx = Transaction.objects.get()
        Transaction.objects.update(created_at=timezone.now() - timedelta(hours=1))
        assert sweep_stale_pending_transactions() == 1
        payment_id, gateway = _gateway_for(tx, "succeeded")

        with pytest.raises(Exception, match="успех пришёл после истечения TTL"):
            confirm_payment(
                payment_id=payment_id,
                gateway=gateway,
                schedule_port=FakeSchedulePort(),
            )

        tx.refresh_from_db()
        assert tx.requires_compensation is True
        assert (
            Enrollment.objects.get(type=EnrollmentType.TRIAL).status
            == EnrollmentStatus.CANCELED
        )


@pytest.mark.django_db
class TestCheckoutTrialView:
    def _post(
        self,
        user: Parent,
        body: dict[str, object],
        port: FakeSchedulePort,
        monkeypatch: pytest.MonkeyPatch,
    ):  # noqa: ANN201 — DRF Response, тесты вне mypy-скоупа
        monkeypatch.setattr("apps.billing.views.resolve_schedule_port", lambda: port)
        monkeypatch.setattr("apps.billing.views.YookassaHttpGateway", FakeGateway)
        request = APIRequestFactory().post(
            "/api/v1/checkout/trial",
            body,
            format="json",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        force_authenticate(request, user=user)
        return CheckoutTrialView.as_view()(request)

    def test_happy_path_returns_201_with_payment_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        trial_date = timezone.localdate() + timedelta(days=3)

        response = self._post(
            parent,
            {
                "student_id": student.pk,
                "schedule_id": 101,
                "trial_date": trial_date.isoformat(),
            },
            _trial_port(101, trial_date),
            monkeypatch,
        )

        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert response.data["status"] == "PENDING_PAYMENT"
        assert Enrollment.objects.get().type == EnrollmentType.TRIAL

    def test_limit_conflict_maps_to_409_with_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        _trial_checkout(101, parent=parent, student=student)
        trial_date = timezone.localdate() + timedelta(days=4)

        response = self._post(
            parent,
            {
                "student_id": student.pk,
                "schedule_id": 101,
                "trial_date": trial_date.isoformat(),
            },
            _trial_port(101, trial_date),
            monkeypatch,
        )
        response.render()

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["type"] == "urn:problem-type:triallimitconflict"
        assert response.data["code"] == "TRIAL_LIMIT_EXCEEDED"

    def test_trial_over_subscription_maps_to_409_already_enrolled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        _checkout([101], parent=parent, student=student)
        trial_date = timezone.localdate() + timedelta(days=3)

        response = self._post(
            parent,
            {
                "student_id": student.pk,
                "schedule_id": 101,
                "trial_date": trial_date.isoformat(),
            },
            _trial_port(101, trial_date),
            monkeypatch,
        )
        response.render()

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["type"] == "urn:problem-type:enrollmentconflict"
        assert response.data["code"] == "STUDENT_ALREADY_ENROLLED"


@pytest.mark.django_db
class TestSeatTimeAxis:
    # !!!: занятость имеет ось времени. Регресс-тесты на класс ошибок
    # «плоский count() схлопывает разные даты в одну загрузку слота»

    def _trial_on(self, offset_days: int, *, capacity: int = 10) -> None:
        _trial_checkout(101, days_ahead=offset_days, capacity=capacity, price=0)

    def test_trials_on_distinct_dates_do_not_sell_out_the_group(self) -> None:
        # Десять пробных на десять разных недель — это один занятый стул
        # в каждый из дней, а не sold-out группы на все даты
        for week in range(1, 11):
            self._trial_on(week * 7)

        assert Enrollment.objects.filter(type=EnrollmentType.TRIAL).count() == 10
        assert _occupied_seats(101) == 1
        assert (
            _occupied_seats(101, on_date=timezone.localdate() + timedelta(days=7)) == 1
        )

    def test_trials_on_same_date_stack(self) -> None:
        same_day = timezone.localdate() + timedelta(days=7)
        for _ in range(3):
            self._trial_on(7)

        assert _occupied_seats(101, on_date=same_day) == 3
        # для абонемента считается пик по горизонту — те же три
        assert _occupied_seats(101) == 3

    def test_subscription_sees_peak_trial_day_not_the_sum(self) -> None:
        self._trial_on(7)
        self._trial_on(14)
        self._trial_on(14)

        # сумма пробных 3, но пик приходится на один день и равен 2
        assert _occupied_seats(101) == 2

    def test_regular_seats_add_to_every_date(self) -> None:
        EnrollmentFactory(schedule_id=101)
        self._trial_on(7)

        target = timezone.localdate() + timedelta(days=7)
        assert _occupied_seats(101, on_date=target) == 2
        # в день без пробного постоянная запись всё равно держит место
        assert _occupied_seats(101, on_date=target + timedelta(days=7)) == 1

    def test_trial_beyond_subscription_horizon_is_ignored(self) -> None:
        # Пробное через полгода не должно вечно резервировать место
        # под каждый продаваемый абонемент
        self._trial_on(180)

        assert _occupied_seats(101) == 0
        assert (
            _occupied_seats(101, on_date=timezone.localdate() + timedelta(days=180))
            == 1
        )


@pytest.mark.django_db
class TestCheckoutQueryBudget:
    # !!!: подсчёт занятости выполняется под захваченными advisory-локами.
    # Число запросов обязано быть константой от размера корзины, иначе локи
    # простаивают на пачке round-trip'ов, а горячий контур чекаута деградирует

    def _checkout_slots(self, slot_ids: list[int]) -> None:
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        plan = SubscriptionPlanFactory(slots_count=len(slot_ids))
        create_payment(
            parent.pk,
            plan.pk,
            student.pk,
            slot_ids,
            str(uuid.uuid4()),
            _FP,
            gateway=FakeGateway(),
            schedule_port=FakeSchedulePort(),
        )

    def test_seat_counting_does_not_scale_with_cart_size(self) -> None:
        # Разница между корзинами обязана состоять только из INSERT'ов брони
        # (по одному на слот) — счётные запросы в обе стороны одинаковы
        with CaptureQueriesContext(connection) as captured_two:
            self._checkout_slots([101, 102])
        with CaptureQueriesContext(connection) as captured_four:
            self._checkout_slots([103, 104, 105, 106])

        # Локи берутся одним statement, занятость — двумя запросами на весь
        # набор; масштабируются только INSERT'ы брони, по одному на слот
        extra = len(captured_four) - len(captured_two)
        assert extra == 2, f"рост на 2 слота дал +{extra} запросов вместо +2 INSERT'ов"

    def test_bulk_occupancy_uses_two_queries(
        self,
        django_assert_num_queries: DjangoAssertNumQueries,
    ) -> None:
        for slot in (101, 102, 103, 104):
            EnrollmentFactory(schedule_id=slot)

        with django_assert_num_queries(2):
            occupied = _occupied_seats_bulk([101, 102, 103, 104])

        assert occupied == {101: 1, 102: 1, 103: 1, 104: 1}

    def test_bulk_occupancy_on_date_uses_two_queries(
        self,
        django_assert_num_queries: DjangoAssertNumQueries,
    ) -> None:
        target = timezone.localdate() + timedelta(days=3)

        with django_assert_num_queries(2):
            _occupied_seats_bulk([101, 102], on_date=target)


@pytest.mark.django_db
class TestTrialAndSubscriptionSameGroup:
    def _pass_trial(self) -> None:
        # Время прошло: занятие пробного уже состоялось
        Enrollment.objects.filter(type=EnrollmentType.TRIAL).update(
            trial_date=timezone.localdate() - timedelta(days=1)
        )

    def test_subscription_after_past_trial_in_same_slot(self) -> None:
        _, parent, student = _trial_checkout(101, price=0, days_ahead=0)
        self._pass_trial()

        result = _checkout([101], parent=parent, student=student)

        assert result.status == "PENDING_PAYMENT"
        regular = Enrollment.objects.get(student=student, type=EnrollmentType.REGULAR)
        assert regular.schedule_id == 101
        assert regular.status == EnrollmentStatus.HELD

    def test_trial_limit_still_holds_after_trial_passed(self) -> None:
        _, parent, student = _trial_checkout(101, price=0, days_ahead=0)
        self._pass_trial()

        with pytest.raises(TrialLimitExceededError):
            _trial_checkout(101, parent=parent, student=student, days_ahead=7)

    def test_second_subscription_in_same_slot_is_still_rejected(self) -> None:
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        _checkout([101], parent=parent, student=student)

        with pytest.raises(DuplicateEnrollmentError):
            _checkout([101], parent=parent, student=student)

    def test_trial_over_live_subscription_is_rejected(self) -> None:
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        _checkout([101], parent=parent, student=student)

        with pytest.raises(DuplicateEnrollmentError):
            _trial_checkout(101, parent=parent, student=student)

        assert not Enrollment.objects.filter(type=EnrollmentType.TRIAL).exists()

    def test_trial_allowed_after_subscription_canceled(self) -> None:
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        _checkout([101], parent=parent, student=student)
        Enrollment.objects.update(status=EnrollmentStatus.CANCELED)

        result, _, _ = _trial_checkout(101, parent=parent, student=student)

        assert result.status == "PENDING_PAYMENT"

    def test_trial_in_other_group_of_same_activity_is_allowed(self) -> None:
        # Запрет — на ту же группу, а не на кружок целиком
        Schedule.objects.filter(pk=102).update(
            activity_id=Schedule.objects.get(pk=101).activity_id
        )
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        _checkout([101], parent=parent, student=student)

        result, _, _ = _trial_checkout(102, parent=parent, student=student)

        assert result.status == "PENDING_PAYMENT"

    def test_trial_lesson_then_subscription_end_to_end(self) -> None:
        # Купил пробное → вебхук → занятие прошло в журнале → купил абонемент
        # в ту же группу → вебхук → постоянная запись активна
        schedule = Schedule.objects.get(pk=101)
        today = timezone.localdate()
        days_ahead = (schedule.day_of_week - today.weekday()) % 7
        trial_date = today + timedelta(days=days_ahead)

        _, parent, student = _trial_checkout(101, days_ahead=days_ahead)
        trial_tx = Transaction.objects.get()
        payment_id, gateway = _gateway_for(trial_tx, "succeeded")
        confirm_payment(
            payment_id=payment_id,
            gateway=gateway,
            schedule_port=_trial_port(101, trial_date),
        )
        trial = Enrollment.objects.get(type=EnrollmentType.TRIAL)
        assert trial.status == EnrollmentStatus.ENROLLED

        open_lesson(101, trial_date)
        attendance = Attendance.objects.get(enrollment=trial)
        assert attendance.status == AttendanceStatus.ATTENDED
        self._pass_trial()

        _checkout([101], parent=parent, student=student)
        sub_tx = Transaction.objects.get(subscription__isnull=False)
        payment_id, gateway = _gateway_for(sub_tx, "succeeded")
        confirm_payment(
            payment_id=payment_id,
            gateway=gateway,
            schedule_port=FakeSchedulePort(),
        )

        regular = Enrollment.objects.get(type=EnrollmentType.REGULAR)
        assert regular.student_id == student.pk
        assert regular.schedule_id == 101
        assert regular.status == EnrollmentStatus.ENROLLED
        assert regular.subscription is not None
        assert regular.subscription.status == SubscriptionStatus.ACTIVE
        trial.refresh_from_db()
        # Пробное не отменяется — на нём держится лимит «1 пробное на кружок»
        assert trial.status == EnrollmentStatus.ENROLLED
