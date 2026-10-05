from __future__ import annotations

import datetime
import uuid

import pytest
from django.utils import timezone

from apps.billing.models import (
    Attendance,
    AttendanceStatus,
    Enrollment,
    EnrollmentType,
    Subscription,
    SubscriptionSlot,
    Transaction,
)
from apps.billing.services import (
    TrialDateUnavailableError,
    _month_after,
    confirm_payment,
    create_payment,
    create_trial_payment,
)
from apps.billing.tests.test_billing import (
    FakeGateway,
    ParentDepositFactory,
    _gateway_for,
)
from apps.journal.services import debit_attended_lessons, materialize_today_lessons
from apps.schedule.models import MaskType, Schedule
from apps.schedule.ports import DjangoSchedulePort
from apps.schedule.tests.factories import (
    ScheduleFactory,
    ScheduleMaskFactory,
    StudentFactory,
    SubscriptionPlanFactory,
    TimeSlotFactory,
)
from apps.users.models import Student
from apps.events.ports import DjangoEventBookingPort

pytestmark = pytest.mark.django_db

# ПОЧЕМУ: «сейчас» задаётся явно, даты — от будущего понедельника, чтобы
# проверки «дата в прошлом» не зависели от дня запуска
_TODAY = timezone.localdate()
MONDAY = _TODAY + datetime.timedelta(days=7 - _TODAY.weekday())
WEDNESDAY = MONDAY + datetime.timedelta(days=2)
WEEK = datetime.timedelta(weeks=1)


def _at(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    return timezone.make_aware(
        datetime.datetime.combine(day, datetime.time(hour, minute))
    )


def _freeze(monkeypatch: pytest.MonkeyPatch, moment: datetime.datetime) -> None:
    # localdate()/localtime() без аргумента берут timezone.now() — подмены
    # одной функции хватает на оба «сегодня»
    monkeypatch.setattr(timezone, "now", lambda: moment)


def _monday_group() -> Schedule:
    group: Schedule = ScheduleFactory(
        time_slot=TimeSlotFactory(
            day_of_week=0,
            start_time=datetime.time(16),
            end_time=datetime.time(17),
        )
    )
    return group


def _buy_subscription(
    group: Schedule, *, student: Student | None = None, from_deposit: bool = False
) -> Subscription:
    port = DjangoSchedulePort()
    child = student if student is not None else StudentFactory()
    plan = SubscriptionPlanFactory(slots_count=1, price=700_000)
    if from_deposit:
        ParentDepositFactory(parent=child.parent, balance=plan.price)
    create_payment(
        child.parent_id,
        plan.pk,
        child.pk,
        [group.pk],
        str(uuid.uuid4()),
        "lesson-cutoff",
        gateway=FakeGateway(),
        schedule_port=port,
        use_deposit=from_deposit,
    )
    if not from_deposit:
        tx = Transaction.objects.get(
            parent_id=child.parent_id, subscription__isnull=False
        )
        payment_id, gateway = _gateway_for(tx, "succeeded")
        confirm_payment(
            payment_id=payment_id,
            gateway=gateway,
            schedule_port=port,
            event_port=DjangoEventBookingPort(),
        )
    return Subscription.objects.get(parent_id=child.parent_id)


def _buy_trial(
    group: Schedule,
    trial_date: datetime.date,
    *,
    student: Student | None = None,
) -> Transaction:
    child = student if student is not None else StudentFactory()
    result = create_trial_payment(
        parent_id=child.parent_id,
        student_id=child.pk,
        schedule_id=group.pk,
        trial_date=trial_date,
        idempotency_key=str(uuid.uuid4()),
        request_fingerprint="lesson-cutoff",
        gateway=FakeGateway(),
        schedule_port=DjangoSchedulePort(),
    )
    return Transaction.objects.get(pk=result.transaction_id)


def _pay(tx: Transaction) -> None:
    payment_id, gateway = _gateway_for(tx, "succeeded")
    confirm_payment(
        payment_id=payment_id,
        gateway=gateway,
        schedule_port=DjangoSchedulePort(),
        event_port=DjangoEventBookingPort(),
    )


def _journal(student: Student, day: datetime.date) -> list[str]:
    return list(
        Attendance.objects.filter(enrollment__student=student, date=day).values_list(
            "status", flat=True
        )
    )


class TestSubscriptionStartsFromUpcomingLesson:
    def test_bought_monday_evening_starts_next_monday(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        group = _monday_group()
        _freeze(monkeypatch, _at(MONDAY, 19))

        subscription = _buy_subscription(group)

        assert subscription.start_date == MONDAY + WEEK
        assert subscription.expires_at is not None
        # Срок — месяц от первого занятия, на неделю дальше, чем сейчас
        assert timezone.localtime(subscription.expires_at).date() == _month_after(
            MONDAY + WEEK
        )

    def test_bought_before_start_keeps_today(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        group = _monday_group()
        _freeze(monkeypatch, _at(MONDAY, 15, 59))

        assert _buy_subscription(group).start_date == MONDAY

    def test_lesson_moved_later_today_still_counts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        group = _monday_group()
        ScheduleMaskFactory(
            schedule=group,
            target_date=MONDAY,
            type=MaskType.RESCHEDULE,
            new_start_time=datetime.time(20),
            new_end_time=datetime.time(21),
        )
        _freeze(monkeypatch, _at(MONDAY, 19))

        assert _buy_subscription(group).start_date == MONDAY

    def test_lesson_moved_to_wednesday_and_finished(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Занятие ПН перенесли на СР 12:00; в СР 19:00 оно уже прошло
        group = _monday_group()
        ScheduleMaskFactory(
            schedule=group,
            target_date=MONDAY,
            type=MaskType.RESCHEDULE,
            new_day_of_week=2,
            new_start_time=datetime.time(12),
            new_end_time=datetime.time(13),
        )
        _freeze(monkeypatch, _at(WEDNESDAY, 19))

        assert _buy_subscription(group).start_date == MONDAY + WEEK


class TestTrialOnStartedLesson:
    def test_finished_lesson_today_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        group = _monday_group()
        _freeze(monkeypatch, _at(MONDAY, 19))

        with pytest.raises(TrialDateUnavailableError):
            _buy_trial(group, MONDAY)

    def test_rejected_from_start_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Граница — решение бизнеса 2026-09-27: «до самого начала» (BOOKING_CUTOFF)
        group = _monday_group()
        _freeze(monkeypatch, _at(MONDAY, 16))

        with pytest.raises(TrialDateUnavailableError):
            _buy_trial(group, MONDAY)

    def test_minute_before_start_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        group = _monday_group()
        _freeze(monkeypatch, _at(MONDAY, 15, 59))

        _buy_trial(group, MONDAY)

    def test_rescheduled_earlier_counts_from_new_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        group = _monday_group()
        ScheduleMaskFactory(
            schedule=group,
            target_date=MONDAY,
            type=MaskType.RESCHEDULE,
            new_start_time=datetime.time(10),
            new_end_time=datetime.time(11),
        )
        _freeze(monkeypatch, _at(MONDAY, 12))

        with pytest.raises(TrialDateUnavailableError):
            _buy_trial(group, MONDAY)


class TestTrialThenSubscriptionSameDay:
    def test_subscription_starts_next_week_and_all_tokens_fit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Утром купили пробное на сегодняшнее занятие, вечером — абонемент
        # в ту же группу: пробное было сегодня, абонемент — со следующего
        group = _monday_group()
        student = StudentFactory()
        _freeze(monkeypatch, _at(MONDAY, 9))
        _pay(_buy_trial(group, MONDAY, student=student))
        _freeze(monkeypatch, _at(MONDAY, 19))

        subscription = _buy_subscription(group, student=student)

        assert subscription.start_date == MONDAY + WEEK
        last_day = _month_after(MONDAY + WEEK)
        # 5 занятий в окне на 4 фишки — пятое запасное
        assert MONDAY + 5 * WEEK <= last_day < MONDAY + 6 * WEEK
        for week in range(1, 5):
            day = MONDAY + week * WEEK
            _freeze(monkeypatch, _at(day, 7))
            materialize_today_lessons()
            _freeze(monkeypatch, _at(day, 23))
            assert debit_attended_lessons() == 1
        slot = SubscriptionSlot.objects.get(subscription=subscription)
        assert slot.remaining_tokens == 0
        # Сегодняшнее занятие — только пробное, абонементу оно не досталось
        assert set(
            Enrollment.objects.filter(
                student=student, attendances__date=MONDAY
            ).values_list("type", flat=True)
        ) == {EnrollmentType.TRIAL}


class TestJoinTodaysJournal:
    # ПОЧЕМУ: журнал дня собирается в 07:00 — купивший днём до начала
    # занятия должен появиться в нём сразу (решение бизнеса 2026-10-01)

    def test_subscription_before_start_joins_today(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        group = _monday_group()
        student = StudentFactory()
        _freeze(monkeypatch, _at(MONDAY, 7))
        materialize_today_lessons()
        _freeze(monkeypatch, _at(MONDAY, 15))

        subscription = _buy_subscription(group, student=student)

        assert subscription.start_date == MONDAY
        assert _journal(student, MONDAY) == [AttendanceStatus.ATTENDED]

    def test_prepaid_from_deposit_joins_today(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        group = _monday_group()
        student = StudentFactory()
        _freeze(monkeypatch, _at(MONDAY, 15))

        _buy_subscription(group, student=student, from_deposit=True)

        assert _journal(student, MONDAY) == [AttendanceStatus.ATTENDED]

    def test_subscription_after_start_not_in_today(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        group = _monday_group()
        student = StudentFactory()
        _freeze(monkeypatch, _at(MONDAY, 19))

        _buy_subscription(group, student=student)

        assert _journal(student, MONDAY) == []

    def test_morning_journal_does_not_duplicate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Купили ночью, до утренней сборки журнала: отметка одна
        group = _monday_group()
        student = StudentFactory()
        _freeze(monkeypatch, _at(MONDAY, 6))
        _buy_subscription(group, student=student)
        _freeze(monkeypatch, _at(MONDAY, 7))

        materialize_today_lessons()

        assert _journal(student, MONDAY) == [AttendanceStatus.ATTENDED]

    def test_trial_paid_today_joins_today(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        group = _monday_group()
        student = StudentFactory()
        _freeze(monkeypatch, _at(MONDAY, 15, 55))
        tx = _buy_trial(group, MONDAY, student=student)
        # Оплата пришла уже после начала — принимаем (решение 2026-10-01)
        _freeze(monkeypatch, _at(MONDAY, 16, 5))

        _pay(tx)

        assert _journal(student, MONDAY) == [AttendanceStatus.ATTENDED]

    def test_trial_for_future_date_waits_for_morning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        group = _monday_group()
        student = StudentFactory()
        _freeze(monkeypatch, _at(MONDAY, 19))

        _pay(_buy_trial(group, MONDAY + WEEK, student=student))

        assert not Attendance.objects.filter(enrollment__student=student).exists()

    def test_lesson_moved_to_other_day_and_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Занятие ПН перенесли на СР 18:00; покупка в СР 17:00 — успевает
        group = _monday_group()
        ScheduleMaskFactory(
            schedule=group,
            target_date=MONDAY,
            type=MaskType.RESCHEDULE,
            new_day_of_week=2,
            new_start_time=datetime.time(18),
            new_end_time=datetime.time(19),
        )
        student = StudentFactory()
        _freeze(monkeypatch, _at(WEDNESDAY, 17))

        subscription = _buy_subscription(group, student=student)

        assert subscription.start_date == WEDNESDAY
        assert _journal(student, WEDNESDAY) == [AttendanceStatus.ATTENDED]
