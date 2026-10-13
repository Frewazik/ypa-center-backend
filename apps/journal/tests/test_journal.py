from __future__ import annotations

import datetime

import pytest
from django.contrib.admin.sites import AdminSite
from django.http import HttpRequest
from django.test import RequestFactory
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.billing.admin import AttendanceAdmin
from apps.billing.models import (
    Attendance,
    AttendanceStatus,
    Enrollment,
    EnrollmentStatus,
    Subscription,
    SubscriptionStatus,
)
from apps.billing.services import sweep_expired_subscriptions
from apps.journal.admin import LessonAdmin
from apps.journal.models import Lesson
from apps.journal.services import materialize_today_lessons, open_lesson
from apps.journal.tests.factories import LessonFactory
from apps.schedule.models import MaskType, Schedule
from apps.schedule.ports import DjangoSchedulePort
from apps.schedule.services import normalize_week_start
from apps.schedule.tests.factories import (
    EnrollmentFactory,
    ScheduleFactory,
    ScheduleMaskFactory,
    SubscriptionFactory,
    TeacherProfileFactory,
)
from apps.users.models import Parent, TeacherProfile

pytestmark = pytest.mark.django_db


def _next_lesson_date(schedule_day_of_week: int) -> datetime.date:
    today = timezone.localdate()
    return today + datetime.timedelta(days=(schedule_day_of_week - today.weekday()) % 7)


class TestOpenLesson:
    def test_creates_lesson_and_marks_everyone_attended(self) -> None:
        schedule = ScheduleFactory()
        date = _next_lesson_date(schedule.day_of_week)
        enrollments = [
            _active_enrollment(schedule, start_date=date),
            _active_enrollment(schedule, start_date=date),
        ]
        EnrollmentFactory(schedule=schedule, is_active=False)

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
        date = _next_lesson_date(schedule.day_of_week)
        _active_enrollment(schedule, start_date=date)

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

        materialize_today_lessons()

        # группа другого дня недели сегодня не открывается (в окне добора
        # её занятие — 6 дней назад)
        assert list(
            Lesson.objects.filter(date=today).values_list("schedule_id", flat=True)
        ) == [matching.pk]


def _active_enrollment(schedule: Schedule, *, start_date: datetime.date) -> Enrollment:
    subscription = SubscriptionFactory(
        status=SubscriptionStatus.ACTIVE,
        start_date=start_date,
        expires_at=timezone.now() + datetime.timedelta(days=60),
    )
    return EnrollmentFactory(schedule=schedule, subscription=subscription)


class TestMissedMorningCatchUp:
    def test_opens_lesson_of_day_when_task_did_not_run(self) -> None:
        today = timezone.localdate()
        yesterday = today - datetime.timedelta(days=1)
        schedule = ScheduleFactory(time_slot__day_of_week=yesterday.weekday())
        enrollment = _active_enrollment(
            schedule, start_date=yesterday - datetime.timedelta(days=7)
        )

        materialize_today_lessons()

        assert Lesson.objects.filter(schedule=schedule, date=yesterday).exists()
        assert Attendance.objects.get(enrollment=enrollment).date == yesterday

    def test_rerun_does_not_duplicate_marks(self) -> None:
        yesterday = timezone.localdate() - datetime.timedelta(days=1)
        schedule = ScheduleFactory(time_slot__day_of_week=yesterday.weekday())
        _active_enrollment(schedule, start_date=yesterday)

        materialize_today_lessons()
        materialize_today_lessons()

        assert Lesson.objects.filter(schedule=schedule).count() == 1
        assert Attendance.objects.count() == 1

    def test_window_is_one_week(self) -> None:
        today = timezone.localdate()
        schedule = ScheduleFactory(time_slot__day_of_week=today.weekday())
        _active_enrollment(schedule, start_date=today - datetime.timedelta(days=14))

        materialize_today_lessons()

        assert list(Lesson.objects.values_list("date", flat=True)) == [today]

    def test_already_opened_lesson_is_not_reopened(self) -> None:
        # ПОЧЕМУ: купивший абонемент после начала занятия впервые придёт через
        # неделю — повторное открытие вчерашнего занятия вписало бы его туда
        # и ночью сожгло бы фишку
        yesterday = timezone.localdate() - datetime.timedelta(days=1)
        schedule = ScheduleFactory(time_slot__day_of_week=yesterday.weekday())
        LessonFactory(schedule=schedule, date=yesterday)
        _active_enrollment(schedule, start_date=yesterday)

        materialize_today_lessons()

        assert not Attendance.objects.exists()

    def test_subscription_started_later_is_not_marked(self) -> None:
        today = timezone.localdate()
        yesterday = today - datetime.timedelta(days=1)
        schedule = ScheduleFactory(time_slot__day_of_week=yesterday.weekday())
        _active_enrollment(schedule, start_date=today)

        materialize_today_lessons()

        assert Lesson.objects.filter(schedule=schedule, date=yesterday).exists()
        assert not Attendance.objects.exists()

    def test_expired_subscription_is_not_reopened(self) -> None:
        today = timezone.localdate()
        two_days_ago = today - datetime.timedelta(days=2)
        schedule = ScheduleFactory(time_slot__day_of_week=two_days_ago.weekday())
        enrollment = _active_enrollment(
            schedule, start_date=two_days_ago - datetime.timedelta(days=21)
        )
        Subscription.objects.update(
            expires_at=timezone.now() - datetime.timedelta(days=1)
        )
        sweep_expired_subscriptions(schedule_port=DjangoSchedulePort())

        materialize_today_lessons()

        enrollment.refresh_from_db()
        assert enrollment.status == EnrollmentStatus.CANCELED
        assert not Attendance.objects.exists()

    def test_trial_of_other_day_is_not_marked(self) -> None:
        yesterday = timezone.localdate() - datetime.timedelta(days=1)
        schedule = ScheduleFactory(time_slot__day_of_week=yesterday.weekday())
        EnrollmentFactory(
            schedule=schedule,
            trial=True,
            trial_date=yesterday + datetime.timedelta(days=7),
        )

        materialize_today_lessons()

        assert not Attendance.objects.exists()


def _next_week_monday() -> datetime.date:
    # ПОЧЕМУ от понедельника: перенос двигает занятие внутри недели, и
    # понедельник со средой должны попасть в одну неделю при любом «сегодня»
    return normalize_week_start(timezone.localdate()) + datetime.timedelta(weeks=1)


class TestRescheduleToAnotherDay:
    def test_lesson_opens_on_landing_day_not_on_original(self) -> None:
        monday = _next_week_monday()
        wednesday = monday + datetime.timedelta(days=2)
        schedule = ScheduleFactory(time_slot__day_of_week=monday.weekday())
        regular = _active_enrollment(schedule, start_date=monday)
        trial = EnrollmentFactory(schedule=schedule, trial=True, trial_date=wednesday)
        ScheduleMaskFactory(
            schedule=schedule,
            target_date=monday,
            reschedule=True,
            new_day_of_week=wednesday.weekday(),
        )

        materialize_today_lessons(today=monday)
        assert not Lesson.objects.exists()

        materialize_today_lessons(today=wednesday)

        assert list(Lesson.objects.values_list("date", flat=True)) == [wednesday]
        assert set(Attendance.objects.values_list("enrollment_id", "date")) == {
            (regular.pk, wednesday),
            (trial.pk, wednesday),
        }

    def test_time_only_reschedule_keeps_day(self) -> None:
        monday = _next_week_monday()
        schedule = ScheduleFactory(time_slot__day_of_week=monday.weekday())
        _active_enrollment(schedule, start_date=monday)
        ScheduleMaskFactory(schedule=schedule, target_date=monday, reschedule=True)

        materialize_today_lessons(today=monday)

        assert Attendance.objects.get().date == monday

    def test_open_lesson_rejects_original_day(self) -> None:
        monday = _next_week_monday()
        schedule = ScheduleFactory(time_slot__day_of_week=monday.weekday())
        ScheduleMaskFactory(
            schedule=schedule,
            target_date=monday,
            reschedule=True,
            new_day_of_week=monday.weekday() + 2,
        )

        with pytest.raises(ValidationError):
            open_lesson(schedule.pk, monday)
        open_lesson(schedule.pk, monday + datetime.timedelta(days=2))


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
        own_date = _next_lesson_date(own_schedule.day_of_week)
        own_enrollment = _active_enrollment(own_schedule, start_date=own_date)
        open_lesson(own_schedule.pk, own_date)

        foreign_schedule = ScheduleFactory()
        foreign_date = _next_lesson_date(foreign_schedule.day_of_week)
        _active_enrollment(foreign_schedule, start_date=foreign_date)
        open_lesson(foreign_schedule.pk, foreign_date)

        teacher_user = teacher.user
        teacher_user.is_staff = True
        request = RequestFactory().get("/admin/billing/attendance/")
        request.user = teacher_user

        queryset = AttendanceAdmin(Attendance, AdminSite()).get_queryset(request)

        assert {mark.enrollment_id for mark in queryset} == {own_enrollment.pk}


class TestSubstituteTeacher:
    def _moved_lesson(
        self, *, substitute: TeacherProfile, group_teacher: TeacherProfile
    ) -> tuple[datetime.date, datetime.date]:
        monday = _next_week_monday()
        wednesday = monday + datetime.timedelta(days=2)
        schedule = ScheduleFactory(
            time_slot__day_of_week=monday.weekday(), teacher=group_teacher
        )
        _active_enrollment(schedule, start_date=monday)
        ScheduleMaskFactory(
            schedule=schedule,
            target_date=monday,
            reschedule=True,
            new_day_of_week=wednesday.weekday(),
            new_teacher=substitute,
        )
        open_lesson(schedule.pk, wednesday)
        # следующее занятие группы ведёт уже основной педагог
        open_lesson(schedule.pk, monday + datetime.timedelta(weeks=1))
        return wednesday, monday + datetime.timedelta(weeks=1)

    def _request(self, teacher: TeacherProfile, path: str) -> HttpRequest:
        teacher.user.is_staff = True
        request = RequestFactory().get(path)
        request.user = teacher.user
        return request

    def test_substitute_sees_only_moved_lesson(self) -> None:
        substitute = TeacherProfileFactory()
        wednesday, _ = self._moved_lesson(
            substitute=substitute, group_teacher=TeacherProfileFactory()
        )

        lessons = LessonAdmin(Lesson, AdminSite()).get_queryset(
            self._request(substitute, "/admin/journal/lesson/")
        )
        marks = AttendanceAdmin(Attendance, AdminSite()).get_queryset(
            self._request(substitute, "/admin/billing/attendance/")
        )

        assert [lesson.date for lesson in lessons] == [wednesday]
        assert [mark.date for mark in marks] == [wednesday]

    def test_group_teacher_keeps_seeing_moved_lesson(self) -> None:
        group_teacher = TeacherProfileFactory()
        wednesday, next_monday = self._moved_lesson(
            substitute=TeacherProfileFactory(), group_teacher=group_teacher
        )

        lessons = LessonAdmin(Lesson, AdminSite()).get_queryset(
            self._request(group_teacher, "/admin/journal/lesson/")
        )

        assert {lesson.date for lesson in lessons} == {wednesday, next_monday}
