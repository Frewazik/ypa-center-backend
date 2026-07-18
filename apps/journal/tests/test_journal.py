from __future__ import annotations

import datetime

import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.billing.admin import AttendanceAdmin
from apps.billing.models import Attendance, AttendanceStatus
from apps.journal.admin import LessonAdmin
from apps.journal.models import Lesson
from apps.journal.services import materialize_today_lessons, open_lesson
from apps.journal.tests.factories import LessonFactory
from apps.schedule.models import MaskType
from apps.schedule.tests.factories import (
    EnrollmentFactory,
    ScheduleFactory,
    ScheduleMaskFactory,
    TeacherProfileFactory,
)
from apps.users.models import Parent

pytestmark = pytest.mark.django_db


def _next_lesson_date(schedule_day_of_week: int) -> datetime.date:
    today = timezone.localdate()
    return today + datetime.timedelta(days=(schedule_day_of_week - today.weekday()) % 7)


class TestOpenLesson:
    def test_creates_lesson_and_marks_everyone_attended(self) -> None:
        schedule = ScheduleFactory()
        enrollments = [
            EnrollmentFactory(schedule=schedule),
            EnrollmentFactory(schedule=schedule),
        ]
        EnrollmentFactory(schedule=schedule, is_active=False)
        date = _next_lesson_date(schedule.day_of_week)

        lesson = open_lesson(schedule.pk, date)

        assert lesson.schedule_id == schedule.pk
        marks = Attendance.objects.filter(date=date)
        assert marks.count() == 2
        assert set(marks.values_list("status", flat=True)) == {
            AttendanceStatus.ATTENDED
        }
        assert {mark.enrollment_id for mark in marks} == {
            enrollment.pk for enrollment in enrollments
        }

    def test_is_idempotent(self) -> None:
        schedule = ScheduleFactory()
        EnrollmentFactory(schedule=schedule)
        date = _next_lesson_date(schedule.day_of_week)

        first = open_lesson(schedule.pk, date)
        second = open_lesson(schedule.pk, date)

        assert first.pk == second.pk
        assert Attendance.objects.count() == 1

    def test_rejects_wrong_weekday(self) -> None:
        schedule = ScheduleFactory()
        wrong_date = _next_lesson_date(schedule.day_of_week) + datetime.timedelta(
            days=1
        )

        with pytest.raises(ValidationError):
            open_lesson(schedule.pk, wrong_date)

    def test_rejects_cancelled_date(self) -> None:
        schedule = ScheduleFactory()
        date = _next_lesson_date(schedule.day_of_week)
        ScheduleMaskFactory(
            schedule=schedule, target_date=date, type=MaskType.CANCELLATION
        )

        with pytest.raises(ValidationError):
            open_lesson(schedule.pk, date)


class TestMaterializeToday:
    def test_opens_lessons_for_todays_groups(self) -> None:
        today = timezone.localdate()
        matching = ScheduleFactory(
            time_slot__day_of_week=today.weekday(),
            time_slot__start_time=datetime.time(hour=10),
        )
        EnrollmentFactory(schedule=matching)
        other_day = (today.weekday() + 1) % 7
        ScheduleFactory(
            time_slot__day_of_week=other_day,
            time_slot__start_time=datetime.time(hour=10),
        )

        opened = materialize_today_lessons()

        assert opened == 1
        assert Lesson.objects.filter(schedule=matching, date=today).exists()


class TestTeacherScoping:
    def test_teacher_sees_only_own_lessons(self) -> None:
        teacher = TeacherProfileFactory()
        own = LessonFactory(schedule=ScheduleFactory(teacher=teacher))
        LessonFactory()

        teacher_user = teacher.user
        teacher_user.is_staff = True
        request = RequestFactory().get("/admin/journal/lesson/")
        request.user = teacher_user

        queryset = LessonAdmin(Lesson, AdminSite()).get_queryset(request)

        assert list(queryset) == [own]

    def test_superuser_sees_everything(self) -> None:
        LessonFactory()
        LessonFactory()
        superuser = Parent.objects.create_superuser(
            email="root@example.com", password="secret"
        )
        request = RequestFactory().get("/admin/journal/lesson/")
        request.user = superuser

        queryset = LessonAdmin(Lesson, AdminSite()).get_queryset(request)

        assert queryset.count() == 2

    def test_attendance_scoped_to_teacher_groups(self) -> None:
        teacher = TeacherProfileFactory()
        own_schedule = ScheduleFactory(teacher=teacher)
        own_enrollment = EnrollmentFactory(schedule=own_schedule)
        open_lesson(own_schedule.pk, _next_lesson_date(own_schedule.day_of_week))

        foreign_schedule = ScheduleFactory()
        EnrollmentFactory(schedule=foreign_schedule)
        open_lesson(
            foreign_schedule.pk, _next_lesson_date(foreign_schedule.day_of_week)
        )

        teacher_user = teacher.user
        teacher_user.is_staff = True
        request = RequestFactory().get("/admin/billing/attendance/")
        request.user = teacher_user

        queryset = AttendanceAdmin(Attendance, AdminSite()).get_queryset(request)

        assert {mark.enrollment_id for mark in queryset} == {own_enrollment.pk}
