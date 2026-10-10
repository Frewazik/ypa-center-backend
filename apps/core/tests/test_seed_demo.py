from __future__ import annotations

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from apps.billing.models import (
    Attendance,
    AttendanceStatus,
    Enrollment,
    EnrollmentType,
    Subscription,
    SubscriptionPlan,
    SubscriptionSlot,
    Transaction,
    TransactionStatus,
)
from apps.catalog.models import Activity
from apps.content.models import GalleryImage
from apps.events.models import Event
from apps.schedule.models import Schedule
from apps.users.models import Parent, Student, TeacherProfile

pytestmark = pytest.mark.django_db

# Перевод классов в возраст, подтверждённый заказчиком; у «Помощи с ДЗ»
# классов в материалах центра нет — без ограничения
_EXPECTED_AGES: dict[str, tuple[int | None, int | None]] = {
    "1–9 классы": (7, 16),
    "0 класс": (6, 7),
    "1 класс": (7, 8),
    "2 класс": (8, 9),
    "3–5 классы": (9, 12),
    "Помощь с ДЗ": (None, None),
}


@pytest.fixture
def seeded(settings) -> None:
    settings.DEBUG = True
    call_command("seed_demo")


class TestSeedDemo:
    def test_populates_showcase_and_family(self, seeded: None) -> None:
        assert Activity.objects.count() == 2
        assert Schedule.objects.count() == 22
        assert SubscriptionPlan.objects.count() == 6
        assert Event.objects.filter(is_published=True).count() == 4

        parent = Parent.objects.get(email="parent@demo.ru")
        assert Student.objects.filter(parent=parent).count() == 2
        # ПОЧЕМУ distinct: у абонемента Мирона две записи — JOIN дублирует строку
        subscription = Subscription.objects.distinct().get(
            enrollments__student__full_name="Синяев Мирон"
        )
        assert SubscriptionSlot.objects.filter(subscription=subscription).count() == 2
        # ПОЧЕМУ: без оплат и посещений дашборд и эти разделы админки пусты на демо
        assert Transaction.objects.filter(
            status=TransactionStatus.SUCCEEDED, subscription=subscription
        ).exists()
        assert Attendance.objects.filter(enrollment__subscription=subscription).exists()

        admin = Parent.objects.get(email="admin@demo.ru")
        assert admin.is_superuser
        assert admin.check_password("admin123")

    def test_several_families_with_purchases_and_trials(self, seeded: None) -> None:
        assert Parent.objects.filter(email__endswith="@demo.ru").count() >= 16
        assert Student.objects.count() == 24
        assert Enrollment.objects.filter(type=EnrollmentType.TRIAL).count() == 2

    def test_slot_tokens_match_attended_lessons(self, seeded: None) -> None:
        # ПОЧЕМУ: отметка «присутствовал» обязана списать фишку слота,
        # пропуск — нет; иначе ЛК покажет остаток, не сходящийся с журналом
        for slot in SubscriptionSlot.objects.all():
            attended = Attendance.objects.filter(
                enrollment__subscription_id=slot.subscription_id,
                enrollment__schedule_id=slot.slot_id,
                status=AttendanceStatus.ATTENDED,
                token_debited=True,
            ).count()
            assert slot.remaining_tokens == slot.granted_tokens - attended

    def test_payments_spread_over_last_month(self, seeded: None) -> None:
        days = {
            timezone.localtime(created_at).date()
            for created_at in Transaction.objects.values_list("created_at", flat=True)
        }
        today = timezone.localdate()
        assert len(days) >= 10
        assert all((today - day).days <= 30 for day in days)

    def test_restores_teacher_group_permissions_after_flush(self, settings) -> None:
        # ПОЧЕМУ: flush стирает права, выданные миграцией journal/0002
        Group.objects.filter(name="Учителя").delete()
        settings.DEBUG = True

        call_command("seed_demo")

        group = Group.objects.get(name="Учителя")
        assert group.permissions.filter(codename="change_lesson").exists()
        assert group.permissions.filter(codename="change_attendance").exists()

    def test_teachers_can_log_into_admin(self, seeded: None) -> None:
        for profile in TeacherProfile.objects.select_related("user"):
            assert profile.user.is_staff
            assert profile.user.check_password("teacher123")

    def test_children_fit_age_range_of_their_groups(self, seeded: None) -> None:
        for enrollment in Enrollment.objects.select_related("student", "schedule"):
            schedule = enrollment.schedule
            age = (timezone.localdate() - enrollment.student.dob).days // 365
            if schedule.age_min is not None and schedule.age_max is not None:
                assert schedule.age_min <= age <= schedule.age_max, enrollment

    def test_second_run_is_idempotent(self, seeded: None) -> None:
        call_command("seed_demo")

        assert Activity.objects.count() == 2
        assert Schedule.objects.count() == 22
        assert SubscriptionPlan.objects.count() == 6
        assert Event.objects.count() == 4
        assert GalleryImage.objects.count() == 6
        # ПОЧЕМУ 2: у демо-родителя по абонементу на каждого из двух детей
        assert Subscription.objects.filter(parent__email="parent@demo.ru").count() == 2
        assert Student.objects.count() == 24

    def test_refuses_outside_debug(self) -> None:
        # ПОЧЕМУ: сиды содержат фиксированный пароль админа —
        # на проде команда обязана отказать
        with override_settings(DEBUG=False):
            with pytest.raises(CommandError):
                call_command("seed_demo")

    def test_no_admin_flag_skips_superuser(self, settings) -> None:
        settings.DEBUG = True
        call_command("seed_demo", "--no-admin")
        assert not Parent.objects.filter(email="admin@demo.ru").exists()


class TestSeedDemoContent:
    def test_group_ages_follow_school_grades(self, seeded: None) -> None:
        ages = {
            (group.group_name, group.age_min, group.age_max)
            for group in Schedule.objects.all()
        }
        assert ages == {(name, *age) for name, age in _EXPECTED_AGES.items()}

    def test_group_name_does_not_repeat_activity_name(self, seeded: None) -> None:
        for group in Schedule.objects.select_related("activity"):
            assert group.activity.name not in group.group_name

    def test_thinking_club_is_one_group_with_sixteen_slots(self, seeded: None) -> None:
        slots = Schedule.objects.filter(activity__slug="kruzhok-myshleniya")
        assert slots.count() == 16
        assert set(slots.values_list("group_name", flat=True)) == {"1–9 классы"}

    def test_features_are_full_phrases_and_tags_are_short(self, seeded: None) -> None:
        for activity in Activity.objects.all():
            assert 3 <= len(activity.features) <= 5
            assert 3 <= len(activity.tags) <= 5
            for feature in activity.features:
                assert len(feature.split()) >= 6, feature
            for tag in activity.tags:
                assert len(tag.split()) <= 3, tag

    def test_real_teachers_have_no_borrowed_photo_or_quote(self, seeded: None) -> None:
        teachers = TeacherProfile.objects.select_related("user")
        assert {t.user.full_name for t in teachers} == {
            "Макуха Надежда",
            "Мордвинов Яков",
        }
        for teacher in teachers:
            assert teacher.photo_url == ""
            assert teacher.quote == ""

    def test_board_games_on_upcoming_wednesdays(self, seeded: None) -> None:
        events = list(Event.objects.order_by("start_datetime"))
        assert {e.title for e in events} == {"Настольные игры"}
        for event in events:
            local_start = timezone.localtime(event.start_datetime)
            assert local_start.weekday() == 2
            assert (local_start.hour, local_start.minute) == (17, 0)
            assert event.duration_minutes == 120
            assert event.price == 0
            assert event.start_datetime > timezone.now()
        assert events[0].seats_taken == events[0].capacity == 10

    def test_rerun_retires_old_demo_catalog(self, settings) -> None:
        settings.DEBUG = True
        old = Activity.objects.create(name="Шахматы", slug="shahmaty", is_active=True)

        call_command("seed_demo")

        old.refresh_from_db()
        assert old.is_active is False
