from __future__ import annotations

import datetime
import logging
from datetime import date, timedelta
from itertools import count

import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory
from django.utils import timezone

from apps.billing.admin import AttendanceAdmin
from apps.billing.models import (
    Attendance,
    AttendanceStatus,
    DepositEntry,
    Enrollment,
    ParentDeposit,
    Subscription,
    SubscriptionSlot,
    SubscriptionStatus,
)
from apps.billing.services import (
    TokenNotRefundableError,
    set_attendance_status,
    sweep_expired_subscriptions,
)
from apps.billing.tests.factories import AttendanceFactory, SubscriptionSlotFactory
from apps.journal.services import (
    debit_attended_lessons,
    materialize_today_lessons,
    open_lesson,
)
from apps.schedule.models import MaskType
from apps.schedule.ports import DjangoSchedulePort
from apps.schedule.services import normalize_week_start
from apps.schedule.tests.factories import (
    EnrollmentFactory,
    ScheduleFactory,
    ScheduleMaskFactory,
    SubscriptionFactory,
)

pytestmark = pytest.mark.django_db

_PRICE = 400_000  # 4 000 ₽ за слот из 4 фишек
_BASE = 100_000  # 1 000 ₽ за занятие
_start_hours = count()


def _enrolled_child(
    *, remaining_tokens: int = 4, day_of_week: int | None = None
) -> Enrollment:
    # ПОЧЕМУ день недели = сегодня: open_lesson проверяет, что дата — день группы
    today = timezone.localdate()
    schedule = ScheduleFactory(
        time_slot__day_of_week=(
            today.weekday() if day_of_week is None else day_of_week
        ),
        time_slot__start_time=datetime.time(hour=8 + next(_start_hours) % 12),
    )
    subscription = SubscriptionFactory(
        status=SubscriptionStatus.ACTIVE,
        purchase_price=_PRICE,
        base_session_price=_BASE,
        start_date=today - timedelta(days=7),
        expires_at=timezone.now() + timedelta(days=20),
    )
    SubscriptionSlotFactory(
        subscription=subscription,
        slot_id=schedule.pk,
        granted_tokens=4,
        remaining_tokens=remaining_tokens,
    )
    return EnrollmentFactory(subscription=subscription, schedule=schedule)


def _open_today(enrollment: Enrollment) -> Attendance:
    # утро: таск открыл занятие, ребёнок по умолчанию «пришёл»
    today = timezone.localdate()
    open_lesson(enrollment.schedule_id, today)
    return Attendance.objects.get(enrollment=enrollment, date=today)


def _remaining() -> int:
    return SubscriptionSlot.objects.get().remaining_tokens


def _expire_now() -> None:
    # ПОЧЕМУ: истекает в 23:59:59 последнего дня — «сегодня» уже прошло
    Subscription.objects.update(expires_at=timezone.now() - timedelta(seconds=1))


class TestNightlyDebit:
    def test_attended_lesson_debits_one_token(self) -> None:
        attendance = _open_today(_enrolled_child())
        # днём педагог подтверждает «Присутствовал» — статус тот же, списания нет
        set_attendance_status(
            attendance_id=attendance.pk, status=AttendanceStatus.ATTENDED
        )
        assert _remaining() == 4

        assert debit_attended_lessons() == 1

        attendance.refresh_from_db()
        assert attendance.token_debited is True
        assert _remaining() == 3

    def test_rerun_does_not_debit_twice(self) -> None:
        _open_today(_enrolled_child())

        debit_attended_lessons()
        assert debit_attended_lessons() == 0

        assert _remaining() == 3

    def test_future_lesson_waits(self) -> None:
        enrollment = _enrolled_child()
        AttendanceFactory(
            enrollment=enrollment,
            date=timezone.localdate() + timedelta(days=7),
            status=AttendanceStatus.ATTENDED,
        )

        assert debit_attended_lessons() == 0
        assert _remaining() == 4

    @pytest.mark.parametrize(
        "status", [AttendanceStatus.ABSENT_OK, AttendanceStatus.ABSENT_ERR]
    )
    def test_absence_keeps_token(self, status: AttendanceStatus) -> None:
        # ПОЧЕМУ: решение бизнеса — списывает только «пришёл», пропуск с любой
        # причиной возвращается деньгами на депозит при истечении
        attendance = _open_today(_enrolled_child())
        set_attendance_status(attendance_id=attendance.pk, status=status)

        assert debit_attended_lessons() == 0
        assert _remaining() == 4

    def test_trial_is_skipped(self) -> None:
        today = timezone.localdate()
        schedule = ScheduleFactory(
            time_slot__day_of_week=today.weekday(),
            time_slot__start_time=datetime.time(hour=8 + next(_start_hours) % 12),
        )
        EnrollmentFactory(schedule=schedule, trial=True, trial_date=today)
        open_lesson(schedule.pk, today)

        assert debit_attended_lessons() == 0
        assert Attendance.objects.get().token_debited is False

    def test_slot_without_tokens_is_skipped_and_does_not_stop_others(self) -> None:
        # ПОЧЕМУ: в норме занятий 4 на 4 фишки, лишнее — только при сбое
        # (заморозка не на целые недели, смена дня группы); задача не падает
        out_of_tokens = _open_today(_enrolled_child(remaining_tokens=0))
        regular = _open_today(_enrolled_child())

        assert debit_attended_lessons() == 1

        out_of_tokens.refresh_from_db()
        regular.refresh_from_db()
        assert out_of_tokens.token_debited is False
        assert regular.token_debited is True

    def test_expired_by_date_is_left_for_sweeper(self) -> None:
        # ПОЧЕМУ: окно между 23:59:59 и тиком свипера — debit_token откажет,
        # задача не падает, списание доберёт свипер
        _open_today(_enrolled_child())
        _expire_now()

        assert debit_attended_lessons() == 0
        assert _remaining() == 4

    def test_lesson_cancelled_after_opening_burns_nothing(self) -> None:
        enrollment = _enrolled_child()
        attendance = _open_today(enrollment)
        ScheduleMaskFactory(
            schedule=enrollment.schedule,
            target_date=attendance.date,
            type=MaskType.CANCELLATION,
        )

        assert debit_attended_lessons() == 0

        attendance.refresh_from_db()
        assert attendance.status == AttendanceStatus.ABSENT_OK
        assert _remaining() == 4

    def test_lesson_cancelled_after_debit_returns_token(self) -> None:
        enrollment = _enrolled_child()
        attendance = _open_today(enrollment)
        debit_attended_lessons()
        ScheduleMaskFactory(
            schedule=enrollment.schedule,
            target_date=attendance.date,
            type=MaskType.CANCELLATION,
        )

        debit_attended_lessons()

        attendance.refresh_from_db()
        assert attendance.status == AttendanceStatus.ABSENT_OK
        assert attendance.token_debited is False
        assert _remaining() == 4


class TestMissedMorning:
    def test_caught_up_lesson_is_debited_at_night(self) -> None:
        # воспроизведение аудита §2.2: утром вчера задача не запускалась —
        # без добора занятие ушло бы на депозит как непосещённое
        yesterday = timezone.localdate() - timedelta(days=1)
        _enrolled_child(day_of_week=yesterday.weekday())

        materialize_today_lessons()

        assert debit_attended_lessons() == 1
        assert Attendance.objects.get().date == yesterday
        assert _remaining() == 3


class TestRescheduledLessonDebit:
    def _monday_group(self) -> tuple[Enrollment, date, date]:
        monday = normalize_week_start(timezone.localdate()) + timedelta(weeks=1)
        enrollment = _enrolled_child(day_of_week=monday.weekday())
        return enrollment, monday, monday + timedelta(days=2)

    def test_token_is_debited_on_landing_day_only(self) -> None:
        # аудит §2.5: перенос ПН→СР списывал фишку в понедельник за ещё не
        # прошедшее занятие
        enrollment, monday, wednesday = self._monday_group()
        ScheduleMaskFactory(
            schedule=enrollment.schedule,
            target_date=monday,
            reschedule=True,
            new_day_of_week=wednesday.weekday(),
        )

        materialize_today_lessons(today=monday)
        assert debit_attended_lessons(today=monday) == 0

        materialize_today_lessons(today=wednesday)
        assert debit_attended_lessons(today=wednesday) == 1
        assert Attendance.objects.get().date == wednesday

    def test_moved_away_after_opening_burns_nothing(self) -> None:
        # журнал открыт в 07:00, днём занятие перенесли на среду
        enrollment, monday, wednesday = self._monday_group()
        materialize_today_lessons(today=monday)
        ScheduleMaskFactory(
            schedule=enrollment.schedule,
            target_date=monday,
            reschedule=True,
            new_day_of_week=wednesday.weekday(),
        )

        assert debit_attended_lessons(today=monday) == 0

        attendance = Attendance.objects.get(date=monday)
        assert attendance.status == AttendanceStatus.ABSENT_OK
        assert _remaining() == 4

    def test_time_only_reschedule_is_debited_as_usual(self) -> None:
        enrollment, monday, _ = self._monday_group()
        ScheduleMaskFactory(
            schedule=enrollment.schedule, target_date=monday, reschedule=True
        )

        materialize_today_lessons(today=monday)

        assert debit_attended_lessons(today=monday) == 1


class TestExpiryCatchUp:
    def test_sweeper_skips_cancelled_lesson(self) -> None:
        # аудит §2.6: ночная задача не отработала, занятие отменили —
        # при истечении фишка сгорала, на депозит приходило меньше
        attendance = _open_today(_enrolled_child())
        ScheduleMaskFactory(
            schedule=attendance.enrollment.schedule,
            target_date=attendance.date,
            type=MaskType.CANCELLATION,
        )
        _expire_now()

        sweep_expired_subscriptions(schedule_port=DjangoSchedulePort())

        assert DepositEntry.objects.get().amount == _PRICE
        # следующая ночь снимает «пришёл» без возврата фишки — её не списывали
        debit_attended_lessons()
        attendance.refresh_from_db()
        assert attendance.status == AttendanceStatus.ABSENT_OK
        assert attendance.token_debited is False

    def test_debited_mark_of_expired_is_not_retried(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # списано в 23:00, отменили после, абонемент истёк — вернуть фишку
        # нельзя, и каждую ночь писать об этом warning бессмысленно
        attendance = _open_today(_enrolled_child())
        debit_attended_lessons()
        ScheduleMaskFactory(
            schedule=attendance.enrollment.schedule,
            target_date=attendance.date,
            type=MaskType.CANCELLATION,
        )
        _expire_now()
        sweep_expired_subscriptions(schedule_port=DjangoSchedulePort())
        caplog.set_level(logging.WARNING, logger="apps.journal.services")

        debit_attended_lessons()

        assert not caplog.records

    def test_sweeper_debits_lesson_missed_by_nightly_job(self) -> None:
        # воспроизведение аудита §2.3: ходил, а на депозит пришла полная цена
        attendance = _open_today(_enrolled_child())
        _expire_now()

        sweep_expired_subscriptions(schedule_port=DjangoSchedulePort())

        attendance.refresh_from_db()
        entry = DepositEntry.objects.get()
        # ребёнок сходил 1 раз → вернуть 4 000 − 1 × 1 000 = 3 000 ₽
        assert entry.amount == _PRICE - _BASE
        assert attendance.token_debited is True

    def test_sweeper_keeps_absences_as_money(self) -> None:
        attendance = _open_today(_enrolled_child())
        set_attendance_status(
            attendance_id=attendance.pk, status=AttendanceStatus.ABSENT_OK
        )
        _expire_now()

        sweep_expired_subscriptions(schedule_port=DjangoSchedulePort())

        assert DepositEntry.objects.get().amount == _PRICE

    def test_sweeper_survives_fifth_lesson(self) -> None:
        _open_today(_enrolled_child(remaining_tokens=0))
        _expire_now()

        assert sweep_expired_subscriptions(schedule_port=DjangoSchedulePort()) == 1
        assert Subscription.objects.get().status == SubscriptionStatus.EXPIRED
        assert not DepositEntry.objects.exists()

    def test_correction_after_expiry_is_refused(self) -> None:
        # ПОЧЕМУ: решение бизнеса — после истечения фишку не вернуть,
        # депозит менеджер правит вручную
        attendance = _open_today(_enrolled_child())
        _expire_now()
        sweep_expired_subscriptions(schedule_port=DjangoSchedulePort())

        with pytest.raises(TokenNotRefundableError):
            set_attendance_status(
                attendance_id=attendance.pk, status=AttendanceStatus.ABSENT_OK
            )

        attendance.refresh_from_db()
        assert attendance.status == AttendanceStatus.ATTENDED


class TestMonthEndToEnd:
    def test_month_of_lessons_ends_with_correct_deposit(self) -> None:
        today = timezone.localdate()
        week_ago = today - timedelta(days=7)
        enrollment = _enrolled_child()  # абонемент куплен: 4 фишки, 4 000 ₽

        # неделя 1: занятие прошло, ночью фишка списана
        open_lesson(enrollment.schedule_id, week_ago)
        assert debit_attended_lessons(today=week_ago) == 1
        assert _remaining() == 3

        # неделя 2: занятие прошло, фишка списана
        second = _open_today(enrollment)
        assert debit_attended_lessons() == 1
        assert _remaining() == 2

        # педагог исправил отметку — ребёнка не было, фишка вернулась
        set_attendance_status(
            attendance_id=second.pk, status=AttendanceStatus.ABSENT_OK
        )
        assert _remaining() == 3
        assert debit_attended_lessons() == 0

        # абонемент истёк: сходил 1 раз → 4 000 − 1 000 = 3 000 ₽ на депозит
        _expire_now()
        assert sweep_expired_subscriptions(schedule_port=DjangoSchedulePort()) == 1

        deposit = ParentDeposit.objects.get(
            parent_id=enrollment.subscription.parent_id  # type: ignore[union-attr]
        )
        assert deposit.balance == _PRICE - _BASE
        assert DepositEntry.objects.get().amount == _PRICE - _BASE
        assert _remaining() == 0


class TestAdminStatusBypass:
    def test_status_is_readonly_in_change_form(self, admin_user) -> None:
        # ПОЧЕМУ: форма карточки писала статус напрямую, мимо
        # set_attendance_status — без списания и возврата фишки
        attendance = _open_today(_enrolled_child())
        request = RequestFactory().get("/")
        request.user = admin_user
        model_admin = AttendanceAdmin(Attendance, AdminSite())

        assert "status" in model_admin.get_readonly_fields(request, attendance)
