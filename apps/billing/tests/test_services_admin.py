from __future__ import annotations

import datetime

import pytest
from django.utils import timezone

from apps.billing.models import (
    AttendanceStatus,
    EnrollmentStatus,
    SubscriptionStatus,
)
from apps.billing.services import (
    InvalidFreezePeriodError,
    TokenNotRefundableError,
    bulk_freeze_subscriptions,
    set_attendance_status,
)
from apps.billing.tests.factories import AttendanceFactory, SubscriptionSlotFactory
from apps.schedule.tests.factories import SubscriptionFactory

pytestmark = pytest.mark.django_db

FREEZE_START = datetime.date(2026, 7, 10)
FREEZE_END = datetime.date(2026, 7, 17)


def _active_subscription(**kwargs):
    defaults = {
        "status": SubscriptionStatus.ACTIVE,
        "expires_at": timezone.now() + datetime.timedelta(days=30),
    }
    defaults.update(kwargs)
    return SubscriptionFactory(**defaults)


def _attended_attendance():
    """Отметка ATTENDED со списанной фишкой и валидной цепочкой enrollment→slot."""
    attendance = AttendanceFactory(
        status=AttendanceStatus.ATTENDED,
        token_debited=True,
        enrollment__status=EnrollmentStatus.ENROLLED,
        enrollment__subscription=_active_subscription(),
    )
    SubscriptionSlotFactory(
        subscription=attendance.enrollment.subscription,
        slot_id=attendance.enrollment.schedule_id,
        remaining_tokens=3,
    )
    return attendance


class TestBulkFreezeSubscriptions:
    def test_shifts_expires_at_for_all_selected(self) -> None:
        old_expiry = timezone.now() + datetime.timedelta(days=10)
        first = _active_subscription(expires_at=old_expiry)
        second = _active_subscription(expires_at=old_expiry)

        result = bulk_freeze_subscriptions(
            subscription_ids=[first.pk, second.pk],
            start_date=FREEZE_START,
            end_date=FREEZE_END,
            reason="Каникулы центра",
        )

        first.refresh_from_db()
        second.refresh_from_db()
        assert result.frozen_count == 2
        assert result.frozen_days == 7
        assert result.errors == []
        assert first.expires_at == old_expiry + datetime.timedelta(days=7)
        assert second.expires_at == old_expiry + datetime.timedelta(days=7)

    def test_skips_non_active_and_reports_error(self) -> None:
        active = _active_subscription()
        draft = SubscriptionFactory(status=SubscriptionStatus.DRAFT)

        result = bulk_freeze_subscriptions(
            subscription_ids=[active.pk, draft.pk],
            start_date=FREEZE_START,
            end_date=FREEZE_END,
            reason="Частичная заморозка",
        )

        assert result.frozen_count == 1
        assert len(result.errors) == 1
        assert str(draft.pk) in result.errors[0]

    def test_skips_active_without_expires_at(self) -> None:
        subscription = SubscriptionFactory(
            status=SubscriptionStatus.ACTIVE, expires_at=None
        )

        result = bulk_freeze_subscriptions(
            subscription_ids=[subscription.pk],
            start_date=FREEZE_START,
            end_date=FREEZE_END,
            reason="Нет даты истечения",
        )

        assert result.frozen_count == 0
        assert len(result.errors) == 1

    def test_reports_missing_ids(self) -> None:
        subscription = _active_subscription()
        missing_id = subscription.pk + 10_000

        result = bulk_freeze_subscriptions(
            subscription_ids=[subscription.pk, missing_id],
            start_date=FREEZE_START,
            end_date=FREEZE_END,
            reason="С несуществующим id",
        )

        assert result.frozen_count == 1
        assert any(str(missing_id) in error for error in result.errors)

    def test_rejects_inverted_dates(self) -> None:
        subscription = _active_subscription()

        with pytest.raises(InvalidFreezePeriodError):
            bulk_freeze_subscriptions(
                subscription_ids=[subscription.pk],
                start_date=FREEZE_END,
                end_date=FREEZE_START,
                reason="Ошибка дат",
            )

    def test_rejects_empty_selection(self) -> None:
        with pytest.raises(InvalidFreezePeriodError):
            bulk_freeze_subscriptions(
                subscription_ids=[],
                start_date=FREEZE_START,
                end_date=FREEZE_END,
                reason="Пустой выбор",
            )


class TestSetAttendanceStatus:
    def test_mark_attended_debits_token(self) -> None:
        attendance = AttendanceFactory(
            status=AttendanceStatus.ABSENT_OK,
            enrollment__status=EnrollmentStatus.ENROLLED,
            enrollment__subscription=_active_subscription(),
        )
        slot = SubscriptionSlotFactory(
            subscription=attendance.enrollment.subscription,
            slot_id=attendance.enrollment.schedule_id,
            remaining_tokens=4,
        )

        updated = set_attendance_status(
            attendance_id=attendance.pk, status=AttendanceStatus.ATTENDED
        )

        slot.refresh_from_db()
        assert updated.status == AttendanceStatus.ATTENDED
        assert updated.token_debited is True
        assert slot.remaining_tokens == 3

    def test_unmark_attended_refunds_token(self) -> None:
        attendance = _attended_attendance()
        slot = attendance.enrollment.subscription.slots.get()

        updated = set_attendance_status(
            attendance_id=attendance.pk, status=AttendanceStatus.ABSENT_ERR
        )

        slot.refresh_from_db()
        assert updated.status == AttendanceStatus.ABSENT_ERR
        assert updated.token_debited is False
        assert slot.remaining_tokens == 4

    def test_refund_blocked_on_expired_subscription(self) -> None:
        # ПОЧЕМУ: sweep уже начислил несгораемый остаток на депозит,
        # возврат фишки задним числом разъехался бы с учётом
        attendance = _attended_attendance()
        subscription = attendance.enrollment.subscription
        subscription.status = SubscriptionStatus.EXPIRED
        subscription.save(update_fields=["status"])

        with pytest.raises(TokenNotRefundableError):
            set_attendance_status(
                attendance_id=attendance.pk, status=AttendanceStatus.ABSENT_ERR
            )

    def test_idempotent_on_same_status(self) -> None:
        attendance = _attended_attendance()
        slot = attendance.enrollment.subscription.slots.get()

        updated = set_attendance_status(
            attendance_id=attendance.pk, status=AttendanceStatus.ATTENDED
        )

        slot.refresh_from_db()
        assert updated.token_debited is True
        assert slot.remaining_tokens == 3
