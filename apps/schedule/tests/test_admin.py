from __future__ import annotations

from datetime import date, time, timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.schedule.models import MaskType, Schedule, ScheduleMask, TimeSlot
from apps.schedule.services import build_week_grid, create_schedule_mask
from apps.schedule.tests.factories import (
    ActivityFactory,
    EnrollmentFactory,
    RoomFactory,
    ScheduleFactory,
    ScheduleMaskFactory,
    TeacherProfileFactory,
    TimeSlotFactory,
)

pytestmark = pytest.mark.django_db

_SCHEDULE_ADD_URL = reverse("admin:schedule_schedule_add")
_SCHEDULE_LIST_URL = reverse("admin:schedule_schedule_changelist")
_MASK_ADD_URL = reverse("admin:schedule_schedulemask_add")


def _next_monday() -> date:
    today = timezone.localdate()
    return today + timedelta(days=7 - today.weekday())


def _schedule_form(time_slot: TimeSlot, **overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "activity": ActivityFactory().pk,
        "time_slot": time_slot.pk,
        "teacher": "",
        "room": "",
        "group_name": "Вторая группа",
        "max_capacity": "6",
        "is_active": "on",
        "age_min": "",
        "age_max": "",
    }
    data.update(overrides)
    return data


def _time_slot_form(slot: TimeSlot, **overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "day_of_week": str(slot.day_of_week),
        "start_time": slot.start_time.strftime("%H:%M"),
        "end_time": slot.end_time.strftime("%H:%M"),
    }
    data.update(overrides)
    return data


def _time_slot_change_url(slot: TimeSlot) -> str:
    return reverse("admin:schedule_timeslot_change", args=[slot.pk])


class TestOverlapIsFormError:
    def test_new_group_with_busy_teacher_shows_form_error(
        self, admin_client: Client
    ) -> None:
        teacher = TeacherProfileFactory()
        ScheduleFactory(
            teacher=teacher,
            time_slot=TimeSlotFactory(
                day_of_week=0, start_time=time(8), end_time=time(9)
            ),
        )
        other_slot = TimeSlotFactory(
            day_of_week=0, start_time=time(8), end_time=time(9)
        )

        response = admin_client.post(
            _SCHEDULE_ADD_URL, _schedule_form(other_slot, teacher=teacher.pk)
        )

        assert response.status_code == 200
        assert "Преподаватель занят" in response.content.decode()
        assert Schedule.objects.count() == 1

    def test_new_group_with_busy_room_shows_form_error(
        self, admin_client: Client, settings: SettingsWrapper
    ) -> None:
        settings.SCHEDULE_ROOMS_ENABLED = True
        room = RoomFactory()
        ScheduleFactory(
            room=room,
            time_slot=TimeSlotFactory(
                day_of_week=0, start_time=time(8), end_time=time(9)
            ),
        )
        other_slot = TimeSlotFactory(
            day_of_week=0, start_time=time(8, 30), end_time=time(9, 30)
        )

        response = admin_client.post(
            _SCHEDULE_ADD_URL, _schedule_form(other_slot, room=room.pk)
        )

        assert response.status_code == 200
        assert "Кабинет занят" in response.content.decode()
        assert Schedule.objects.count() == 1

    def test_time_slot_change_overlapping_teacher_shows_form_error(
        self, admin_client: Client
    ) -> None:
        teacher = TeacherProfileFactory()
        ScheduleFactory(
            teacher=teacher,
            time_slot=TimeSlotFactory(
                day_of_week=0, start_time=time(8), end_time=time(9)
            ),
        )
        moved_slot = TimeSlotFactory(
            day_of_week=0, start_time=time(10), end_time=time(11)
        )
        ScheduleFactory(teacher=teacher, time_slot=moved_slot)

        response = admin_client.post(
            _time_slot_change_url(moved_slot),
            _time_slot_form(moved_slot, start_time="08:30", end_time="09:30"),
        )

        assert response.status_code == 200
        assert "Преподаватель занят" in response.content.decode()
        moved_slot.refresh_from_db()
        assert moved_slot.start_time == time(10)


class TestTimeSlotDayChangeKeepsMasks:
    def test_cancellation_survives_time_slot_day_change(
        self, admin_client: Client
    ) -> None:
        monday = _next_monday()
        slot = TimeSlotFactory(day_of_week=0, start_time=time(8), end_time=time(9))
        group = ScheduleFactory(time_slot=slot)
        create_schedule_mask(
            schedule=group, target_date=monday, mask_type=MaskType.CANCELLATION
        )

        response = admin_client.post(
            _time_slot_change_url(slot),
            _time_slot_form(slot, day_of_week="1", confirm_day_change="on"),
        )

        assert "перестанет действовать" in response.content.decode()
        [session] = [s for s in build_week_grid(monday) if s.schedule_id == group.pk]
        assert session.is_cancelled

    def test_cancellation_survives_group_moved_to_other_slot(
        self, admin_client: Client
    ) -> None:
        monday = _next_monday()
        group = ScheduleFactory(
            time_slot=TimeSlotFactory(
                day_of_week=0, start_time=time(8), end_time=time(9)
            ),
            teacher=None,
            room=None,
        )
        create_schedule_mask(
            schedule=group, target_date=monday, mask_type=MaskType.CANCELLATION
        )
        tuesday_slot = TimeSlotFactory(
            day_of_week=1, start_time=time(8), end_time=time(9)
        )

        response = admin_client.post(
            reverse("admin:schedule_schedule_change", args=[group.pk]),
            _schedule_form(
                tuesday_slot,
                activity=group.activity_id,
                group_name=group.group_name,
                confirm_day_change="on",
            ),
        )

        assert "перестанет действовать" in response.content.decode()
        [session] = [s for s in build_week_grid(monday) if s.schedule_id == group.pk]
        assert session.is_cancelled


class TestOverlapOnListActivation:
    # ПОЧЕМУ transaction=True: в обычном тесте всё и так внутри транзакции,
    # и забытая обёртка atomic у списка не проявилась бы
    @pytest.mark.django_db(transaction=True)
    def test_activating_overlapping_group_in_list_shows_error(
        self, admin_client: Client
    ) -> None:
        teacher = TeacherProfileFactory()
        ScheduleFactory(
            teacher=teacher,
            time_slot=TimeSlotFactory(
                day_of_week=0, start_time=time(8), end_time=time(9)
            ),
        )
        idle = ScheduleFactory(
            teacher=teacher,
            is_active=False,
            time_slot=TimeSlotFactory(
                day_of_week=0, start_time=time(8), end_time=time(9)
            ),
        )

        response = admin_client.post(
            _SCHEDULE_LIST_URL,
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "1",
                "form-0-id": str(idle.pk),
                "form-0-age_min": "",
                "form-0-age_max": "",
                "form-0-max_capacity": "6",
                "form-0-is_active": "on",
                "_save": "Сохранить",
            },
        )

        assert response.status_code == 200
        assert "Преподаватель занят" in response.content.decode()
        idle.refresh_from_db()
        assert not idle.is_active


class TestDayChangeConfirmation:
    def test_day_change_lists_families_and_waits_for_confirmation(
        self, admin_client: Client
    ) -> None:
        slot = TimeSlotFactory(day_of_week=0, start_time=time(8), end_time=time(9))
        enrollment = EnrollmentFactory(schedule=ScheduleFactory(time_slot=slot))

        response = admin_client.post(
            _time_slot_change_url(slot), _time_slot_form(slot, day_of_week="1")
        )

        assert response.status_code == 200
        page = response.content.decode()
        assert "Да, переносим" in page
        assert enrollment.student.full_name in page
        assert enrollment.student.parent.email in page
        slot.refresh_from_db()
        assert slot.day_of_week == 0

    def test_confirmed_day_change_moves_groups(self, admin_client: Client) -> None:
        slot = TimeSlotFactory(day_of_week=0, start_time=time(8), end_time=time(9))
        group = ScheduleFactory(time_slot=slot)

        response = admin_client.post(
            _time_slot_change_url(slot),
            _time_slot_form(slot, day_of_week="1", confirm_day_change="on"),
        )

        assert response.status_code == 302
        group.refresh_from_db()
        assert group.day_of_week == 1

    def test_time_change_on_same_day_keeps_masks_without_confirmation(
        self, admin_client: Client
    ) -> None:
        slot = TimeSlotFactory(day_of_week=0, start_time=time(8), end_time=time(9))
        group = ScheduleFactory(time_slot=slot)
        ScheduleMaskFactory(schedule=group, target_date=_next_monday())

        response = admin_client.post(
            _time_slot_change_url(slot),
            _time_slot_form(slot, start_time="10:00", end_time="11:00"),
        )

        assert response.status_code == 302
        group.refresh_from_db()
        assert group.start_time == time(10)


def _mask_form(group: Schedule, **overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "schedule": group.pk,
        "target_date": _next_monday().isoformat(),
        "type": MaskType.CANCELLATION,
        "new_day_of_week": "",
        "new_start_time": "",
        "new_end_time": "",
        "new_teacher": "",
    }
    data.update(overrides)
    return data


def _monday_group(**kwargs: object) -> Schedule:
    group: Schedule = ScheduleFactory(
        time_slot=TimeSlotFactory(day_of_week=0, start_time=time(8), end_time=time(9)),
        **kwargs,
    )
    return group


class TestMaskAdmin:
    def test_cancellation_saved_through_service_and_opens_card(
        self, admin_client: Client
    ) -> None:
        group = _monday_group()

        response = admin_client.post(_MASK_ADD_URL, _mask_form(group))

        mask = ScheduleMask.objects.get()
        assert response.status_code == 302
        assert response["Location"] == reverse(
            "admin:schedule_schedulemask_change", args=[mask.pk]
        )
        assert mask.target_date == _next_monday()

    def test_past_date_rejected_by_service(self, admin_client: Client) -> None:
        group = _monday_group()
        today = timezone.localdate()
        last_monday = today - timedelta(days=today.weekday() or 7)

        response = admin_client.post(
            _MASK_ADD_URL, _mask_form(group, target_date=last_monday.isoformat())
        )

        assert response.status_code == 200
        assert "прошедшую дату" in response.content.decode()
        assert not ScheduleMask.objects.exists()

    def test_reschedule_onto_busy_teacher_shows_service_error(
        self, admin_client: Client
    ) -> None:
        teacher = TeacherProfileFactory()
        group = _monday_group(teacher=teacher)
        ScheduleFactory(
            teacher=teacher,
            time_slot=TimeSlotFactory(
                day_of_week=0, start_time=time(18), end_time=time(19)
            ),
        )

        response = admin_client.post(
            _MASK_ADD_URL,
            _mask_form(
                group,
                type=MaskType.RESCHEDULE,
                new_start_time="18:00",
                new_end_time="19:00",
            ),
        )

        assert response.status_code == 200
        assert "Преподаватель занят в это время на эту дату" in (
            response.content.decode()
        )
        assert not ScheduleMask.objects.exists()

    def test_card_lists_families_and_trial_refund(self, admin_client: Client) -> None:
        group = _monday_group()
        regular = EnrollmentFactory(schedule=group)
        trial = EnrollmentFactory(schedule=group, trial=True, trial_date=_next_monday())
        mask = ScheduleMaskFactory(schedule=group, target_date=_next_monday())

        response = admin_client.get(
            reverse("admin:schedule_schedulemask_change", args=[mask.pk])
        )

        page = response.content.decode()
        assert regular.student.full_name in page
        assert trial.student.full_name in page
        assert "вернуть оплату вручную" in page

    def test_future_mask_deleted_through_admin(self, admin_client: Client) -> None:
        mask = ScheduleMaskFactory(schedule=_monday_group(), target_date=_next_monday())

        response = admin_client.post(
            reverse("admin:schedule_schedulemask_delete", args=[mask.pk]),
            {"post": "yes"},
        )

        assert response.status_code == 302
        assert response["Location"] == reverse("admin:schedule_schedulemask_changelist")
        assert not ScheduleMask.objects.exists()

    def test_todays_mask_cannot_be_deleted(self, admin_client: Client) -> None:
        mask = ScheduleMaskFactory(target_date=timezone.localdate())

        response = admin_client.post(
            reverse("admin:schedule_schedulemask_delete", args=[mask.pk]),
            {"post": "yes"},
        )

        assert response.status_code == 403
        assert ScheduleMask.objects.exists()

    def test_group_card_links_to_mask_form(self, admin_client: Client) -> None:
        group = _monday_group()

        response = admin_client.get(
            reverse("admin:schedule_schedule_change", args=[group.pk])
        )

        assert "Перенести или отменить занятие" in response.content.decode()


class TestRoomsSwitch:
    def test_rooms_hidden_when_disabled(self, admin_client: Client) -> None:
        page = admin_client.get(_SCHEDULE_ADD_URL).content.decode()
        index = admin_client.get(reverse("admin:index")).content.decode()

        assert 'name="room"' not in page
        assert reverse("admin:schedule_room_changelist") not in index

    def test_rooms_shown_when_enabled(
        self, admin_client: Client, settings: SettingsWrapper
    ) -> None:
        settings.SCHEDULE_ROOMS_ENABLED = True

        page = admin_client.get(_SCHEDULE_ADD_URL).content.decode()
        index = admin_client.get(reverse("admin:index")).content.decode()

        assert 'name="room"' in page
        assert reverse("admin:schedule_room_changelist") in index
