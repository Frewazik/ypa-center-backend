from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status

from apps.billing.models import (
    DepositEntry,
    Enrollment,
    EnrollmentStatus,
    SubscriptionPlan,
    SubscriptionSlot,
    SubscriptionStatus,
    Transaction,
)
from apps.billing.services import (
    PlanSlotsMismatchError,
    PlanUnavailableError,
    confirm_payment,
    sweep_expired_subscriptions,
)
from apps.billing.tests import test_billing
from apps.billing.tests.test_billing import (
    FakeSchedulePort,
    ParentFactory,
    StudentFactory,
    SubscriptionFactory,
    SubscriptionPlanFactory,
    SubscriptionSlotFactory,
    _checkout,
    _gateway_for,
)
from apps.core.management.commands.seed_demo import Command as SeedDemo
from apps.schedule.tests.factories import ScheduleFactory

# ПОЧЕМУ 100–106 + свои: пакетный conftest сеет 7 слотов, безлимиту нужно до 10
_SLOTS = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109]


def _unlimited_plan() -> SubscriptionPlan:
    # Как в сидах: возврат при истечении — по базовой цене занятия (1 200 ₽)
    return SubscriptionPlanFactory(
        name="Безлимит",
        slots_count=6,
        price=1_500_000,
        base_session_price=120_000,
        is_unlimited=True,
    )


def _seed_extra_slots() -> None:
    for slot_id in (107, 108, 109):
        ScheduleFactory(id=slot_id)


@pytest.mark.django_db
class TestUnlimitedPlanSlots:
    @pytest.mark.parametrize("slots", [6, 7, 10])
    def test_unlimited_accepts_six_or_more_slots(self, slots: int) -> None:
        _seed_extra_slots()
        plan = _unlimited_plan()

        result = _checkout(_SLOTS[:slots], plan=plan)

        assert result.status == "PENDING_PAYMENT"
        assert Transaction.objects.get().amount == 1_500_000
        assert Enrollment.objects.count() == slots

    def test_unlimited_rejects_fewer_slots(self) -> None:
        plan = _unlimited_plan()

        with pytest.raises(PlanSlotsMismatchError, match="от 6"):
            _checkout(_SLOTS[:5], plan=plan)
        assert Transaction.objects.count() == 0

    def test_regular_plan_still_requires_exact_count(self) -> None:
        plan = SubscriptionPlanFactory(slots_count=2)

        with pytest.raises(PlanSlotsMismatchError):
            _checkout(_SLOTS[:3], plan=plan)

    @pytest.mark.parametrize("slots", [6, 7, 10])
    def test_end_to_end_same_unlimited_price_and_four_tokens_per_slot(
        self, slots: int
    ) -> None:
        # Сквозной: чекаут → вебхук «оплачено» → абонемент активен
        _seed_extra_slots()
        plan = _unlimited_plan()
        parent = ParentFactory()
        student = StudentFactory(parent=parent)

        _checkout(_SLOTS[:slots], plan=plan, parent=parent, student=student)
        tx = Transaction.objects.get()
        payment_id, gateway = _gateway_for(tx, "succeeded")
        confirm_payment(
            payment_id=payment_id, gateway=gateway, schedule_port=FakeSchedulePort()
        )

        tx.refresh_from_db()
        subscription = tx.subscription
        assert subscription is not None
        assert subscription.plan_id == plan.pk
        assert subscription.status == SubscriptionStatus.ACTIVE
        assert subscription.purchase_price == 1_500_000
        assert tx.amount == 1_500_000
        tokens = list(
            SubscriptionSlot.objects.filter(subscription=subscription).values_list(
                "granted_tokens", flat=True
            )
        )
        assert tokens == [4] * slots
        assert (
            Enrollment.objects.filter(status=EnrollmentStatus.ENROLLED).count() == slots
        )


@pytest.mark.django_db
class TestUnlimitedExpiryCredit:
    def test_unused_money_counted_at_single_lesson_price(self) -> None:
        # Решение бизнеса: возврат без скидки тарифа — 10 посещений
        # по 1 200 ₽ = 12 000, на депозит 15 000 − 12 000 = 3 000 ₽
        subscription = SubscriptionFactory(
            plan=_unlimited_plan(),
            status=SubscriptionStatus.ACTIVE,
            expires_at=timezone.now() - timedelta(days=1),
        )
        remaining = [0, 0, 2, 4, 4, 4, 4, 4, 4, 4]  # из 40 фишек потрачено 10
        for slot_id, left in zip(_SLOTS, remaining, strict=True):
            SubscriptionSlotFactory(
                subscription=subscription,
                slot_id=slot_id,
                granted_tokens=4,
                remaining_tokens=left,
            )

        sweep_expired_subscriptions()

        assert DepositEntry.objects.get().amount == 300_000


@pytest.mark.django_db
class TestInactivePlan:
    def test_inactive_plan_cannot_be_bought(self) -> None:
        plan = SubscriptionPlanFactory(slots_count=1, is_active=False)

        with pytest.raises(PlanUnavailableError):
            _checkout([101], plan=plan)
        assert Transaction.objects.count() == 0

    def test_view_answers_409_plan_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = test_billing.TestCheckoutViewSecurity()
        api._stub_boundaries(monkeypatch)
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        plan = SubscriptionPlanFactory(slots_count=1, is_active=False)

        response = api._post(
            parent, {"plan_id": plan.pk, "student_id": student.pk, "slot_ids": [101]}
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["detail"].code == "PLAN_UNAVAILABLE"


@pytest.mark.django_db
class TestPlanConstraints:
    def test_second_active_plan_for_same_slots_count_rejected(self) -> None:
        SubscriptionPlanFactory(slots_count=2)

        with pytest.raises(IntegrityError), transaction.atomic():
            SubscriptionPlanFactory(slots_count=2)

    def test_inactive_duplicate_is_allowed(self) -> None:
        # Замена тарифа: старый снят с продажи, новый активен
        SubscriptionPlanFactory(slots_count=2, is_active=False)
        SubscriptionPlanFactory(slots_count=2)

        assert SubscriptionPlan.objects.filter(slots_count=2).count() == 2

    def test_second_active_unlimited_rejected(self) -> None:
        _unlimited_plan()

        with pytest.raises(IntegrityError), transaction.atomic():
            SubscriptionPlanFactory(slots_count=7, is_unlimited=True)

    def test_regular_plan_may_share_slots_count_with_unlimited(self) -> None:
        _unlimited_plan()
        SubscriptionPlanFactory(slots_count=6)

        assert SubscriptionPlan.objects.filter(slots_count=6).count() == 2

    @pytest.mark.parametrize(
        "fields",
        [{"slots_count": 0}, {"price": -1}, {"base_session_price": -1}],
    )
    def test_check_constraints(self, fields: dict[str, int]) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            SubscriptionPlanFactory(**fields)

    def test_admin_form_gets_readable_message(self) -> None:
        # ПОЧЕМУ full_clean: ModelForm админки валидирует constraints так же —
        # менеджер видит текст ошибки, а не 500
        SubscriptionPlanFactory(slots_count=2)
        duplicate = SubscriptionPlan(
            name="Дубль", slots_count=2, price=1, base_session_price=1
        )

        with pytest.raises(ValidationError, match="сначала снимите"):
            duplicate.full_clean()


@pytest.mark.django_db
class TestSeedPlans:
    def test_every_seed_plan_refunds_at_base_lesson_price(self) -> None:
        # Скидка — только на цену покупки; возврат у всех тарифов по 1 200 ₽
        SeedDemo()._seed_plans()

        unlimited = SubscriptionPlan.objects.get(is_unlimited=True)
        assert unlimited.slots_count == 6
        assert unlimited.price == 1_500_000
        assert set(
            SubscriptionPlan.objects.values_list("base_session_price", flat=True)
        ) == {120_000}


@pytest.mark.django_db
class TestFingerprintSlotOrder:
    def test_same_slots_in_other_order_replay_not_conflict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ПОЧЕМУ экземпляр, а не наследование: иначе pytest перезапустит
        # все тесты TestCheckoutViewSecurity ещё раз в этом модуле
        api = test_billing.TestCheckoutViewSecurity()
        api._stub_boundaries(monkeypatch)
        parent = ParentFactory()
        student = StudentFactory(parent=parent)
        plan = SubscriptionPlanFactory(slots_count=2)
        key = str(uuid.uuid4())
        body = {"plan_id": plan.pk, "student_id": student.pk}

        first = api._post(parent, {**body, "slot_ids": [101, 102]}, key=key)
        again = api._post(parent, {**body, "slot_ids": [102, 101]}, key=key)

        assert first.status_code == status.HTTP_201_CREATED, first.data
        assert again.status_code == status.HTTP_201_CREATED, again.data
        assert again.data["transaction_id"] == first.data["transaction_id"]
        assert Transaction.objects.count() == 1
