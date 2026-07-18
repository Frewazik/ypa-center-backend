from __future__ import annotations

import factory
from django.utils import timezone
from factory.django import DjangoModelFactory

from apps.journal.models import Lesson
from apps.schedule.tests.factories import ScheduleFactory


class LessonFactory(DjangoModelFactory):
    class Meta:
        model = Lesson

    schedule = factory.SubFactory(ScheduleFactory)
    date = factory.LazyFunction(timezone.localdate)
    topic = factory.Sequence(lambda n: f"Тема занятия {n}")
