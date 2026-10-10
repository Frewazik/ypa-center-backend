from __future__ import annotations

from collections.abc import Iterator
from datetime import date, time, timedelta
from unittest.mock import AsyncMock, call, patch

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.utils import timezone
from pytest_django import DjangoCaptureOnCommitCallbacks

from apps.schedule.models import MaskType, Schedule, ScheduleMask
from apps.schedule.services import create_schedule_mask, delete_schedule_mask
from apps.schedule.tasks import send_lesson_change_email_task
from apps.schedule.tests.factories import (
    EnrollmentFactory,
    ScheduleFactory,
    ScheduleMaskFactory,
    StudentFactory,
    TimeSlotFactory,
)

pytestmark = pytest.mark.django_db


def _next_monday() -> date:
    today = timezone.localdate()
    return today + timedelta(days=7 - today.weekday())


def _monday_group() -> Schedule:
    group: Schedule = ScheduleFactory(
        time_slot=TimeSlotFactory(day_of_week=0, start_time=time(8), end_time=time(9))
    )
    return group


@pytest.fixture
def queued() -> Iterator[AsyncMock]:
    # ПОЧЕМУ: подменяется только постановка в брокер — проверяем, какие
    # письма ушли в очередь после коммита
    with patch("apps.schedule.tasks.send_lesson_change_email_task") as task:
        task.kiq = AsyncMock()
        yield task.kiq


class TestFamiliesNotified:
    def test_cancellation_queues_one_letter_per_family(
        self,
        queued: AsyncMock,
        django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    ) -> None:
        group = _monday_group()
        monday = _next_monday()
        regular = EnrollmentFactory(schedule=group)
        sibling = StudentFactory(parent=regular.student.parent)
        EnrollmentFactory(schedule=group, student=sibling)
        trial = EnrollmentFactory(schedule=group, trial=True, trial_date=monday)
        EnrollmentFactory(
            schedule=group, trial=True, trial_date=monday + timedelta(days=7)
        )

        with django_capture_on_commit_callbacks(execute=True):
            create_schedule_mask(
                schedule=group, target_date=monday, mask_type=MaskType.CANCELLATION
            )

        parents = sorted([regular.student.parent_id, trial.student.parent_id])
        assert queued.await_args_list == [
            call(parent_id, group.pk, monday.isoformat(), "created")
            for parent_id in parents
        ]

    def test_deleted_mask_queues_restore_letter(
        self,
        queued: AsyncMock,
        django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    ) -> None:
        group = _monday_group()
        enrollment = EnrollmentFactory(schedule=group)
        mask = ScheduleMaskFactory(schedule=group, target_date=_next_monday())

        with django_capture_on_commit_callbacks(execute=True):
            delete_schedule_mask(mask_id=mask.pk)

        assert not ScheduleMask.objects.exists()
        queued.assert_awaited_once_with(
            enrollment.student.parent_id,
            group.pk,
            _next_monday().isoformat(),
            "removed",
        )

    def test_todays_mask_is_not_deleted(self) -> None:
        mask = ScheduleMaskFactory(target_date=timezone.localdate())

        with pytest.raises(ValidationError):
            delete_schedule_mask(mask_id=mask.pk)

        assert ScheduleMask.objects.filter(pk=mask.pk).exists()


class TestLessonChangeLetter:
    def test_cancellation_letter_promises_trial_refund(self) -> None:
        group = _monday_group()
        trial = EnrollmentFactory(schedule=group, trial=True, trial_date=_next_monday())
        ScheduleMaskFactory(schedule=group, target_date=_next_monday())

        send_lesson_change_email_task.original_func(
            trial.student.parent_id, group.pk, _next_monday().isoformat(), "created"
        )

        [letter] = mail.outbox
        assert letter.to == [trial.student.parent.email]
        assert "отменено" in letter.body
        assert "вернуть оплату пробного" in letter.body

    def test_cancellation_letter_to_subscriber_has_no_refund(self) -> None:
        group = _monday_group()
        enrollment = EnrollmentFactory(schedule=group)
        ScheduleMaskFactory(schedule=group, target_date=_next_monday())

        send_lesson_change_email_task.original_func(
            enrollment.student.parent_id,
            group.pk,
            _next_monday().isoformat(),
            "created",
        )

        [letter] = mail.outbox
        assert "отменено" in letter.body
        assert "пробного" not in letter.body

    def test_reschedule_letter_names_new_time(self) -> None:
        group = _monday_group()
        enrollment = EnrollmentFactory(schedule=group)
        ScheduleMaskFactory(schedule=group, target_date=_next_monday(), reschedule=True)

        send_lesson_change_email_task.original_func(
            enrollment.student.parent_id,
            group.pk,
            _next_monday().isoformat(),
            "created",
        )

        [letter] = mail.outbox
        assert "переносится" in letter.body
        assert "18:00–19:00" in letter.body
        assert "пробного" not in letter.body

    def test_restore_letter_after_mask_removed(self) -> None:
        group = _monday_group()
        enrollment = EnrollmentFactory(schedule=group)

        send_lesson_change_email_task.original_func(
            enrollment.student.parent_id,
            group.pk,
            _next_monday().isoformat(),
            "removed",
        )

        [letter] = mail.outbox
        assert "по обычному расписанию" in letter.body

    def test_stale_letter_skipped_when_mask_already_removed(self) -> None:
        group = _monday_group()
        enrollment = EnrollmentFactory(schedule=group)

        send_lesson_change_email_task.original_func(
            enrollment.student.parent_id,
            group.pk,
            _next_monday().isoformat(),
            "created",
        )

        assert mail.outbox == []
