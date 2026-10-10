from __future__ import annotations

import datetime

import pytest

from apps.schedule.models import Schedule
from apps.schedule.tests.factories import ScheduleFactory, TimeSlotFactory

pytestmark = pytest.mark.django_db


class TestScheduleStr:
    def test_names_group_with_day_and_time(self) -> None:
        schedule = ScheduleFactory(
            group_name="1–9 классы",
            time_slot=TimeSlotFactory(
                day_of_week=0,
                start_time=datetime.time(16, 0),
                end_time=datetime.time(17, 0),
            ),
        )

        assert str(schedule) == "1–9 классы · Пн 16:00"

    def test_unrefreshed_after_insert_shows_name_only(self) -> None:
        # ПОЧЕМУ: день и время пишет триггер БД — объект, которому админка
        # строит сообщение «добавлено», их ещё не видит
        schedule = Schedule(group_name="0 класс")

        assert str(schedule) == "0 класс"
