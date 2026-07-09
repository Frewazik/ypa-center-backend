"""Фабрики Factory Boy домена «Расписание».

Внешние домены подключены строковыми путями моделей
(``Meta.model = "app.Model"`` — factory_boy разрешает их лениво через реестр
Django), поэтому файл не импортирует чужие домены. Метки приложений —
предположение по DDD-разбиению проекта; при другом разбиении меняются только
константы ниже.
"""

from __future__ import annotations

import datetime

import factory
from django.contrib.auth import get_user_model

from apps.schedule.models import MaskType, Room, Schedule, ScheduleMask, TimeSlot

ACTIVITY_MODEL = "catalog.Activity"
TEACHER_MODEL = "users.TeacherProfile"
PARENT_MODEL = "users.Parent"
STUDENT_MODEL = "users.Student"
ENROLLMENT_MODEL = "billing.Enrollment"


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = get_user_model()

    email = factory.Sequence(lambda n: f"user_{n}@example.com")
    full_name = factory.Sequence(lambda n: f"Преподаватель {n}")


class TeacherProfileFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = TEACHER_MODEL

    user = factory.SubFactory(UserFactory)
    middle_name = factory.Sequence(lambda n: f"Отчество{n}")


class ActivityFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ACTIVITY_MODEL

    name = factory.Sequence(lambda n: f"Кружок {n}")
    slug = factory.Sequence(lambda n: f"activity-{n}")
    category = "CLUB"
    price = 400_000  # копейки: 4000.00 руб (`README.md` §5)
    is_active = True


class RoomFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Room

    name = factory.Sequence(lambda n: f"Кабинет {n}")
    is_active = True


class TimeSlotFactory(factory.django.DjangoModelFactory):
    """Комбинации (день, час) уникальны в пределах 84 подряд созданных слотов."""

    class Meta:
        model = TimeSlot

    day_of_week = factory.Sequence(lambda n: n % 7)
    start_time = factory.Sequence(lambda n: datetime.time(hour=8 + (n // 7) % 12))
    end_time = factory.LazyAttribute(
        lambda slot: datetime.time(hour=slot.start_time.hour + 1)
    )


class ScheduleFactory(factory.django.DjangoModelFactory):
    """Каждая группа получает собственных преподавателя, кабинет и слот —
    массовое создание никогда не задевает exclusion-констрейнты."""

    class Meta:
        model = Schedule

    activity = factory.SubFactory(ActivityFactory)
    time_slot = factory.SubFactory(TimeSlotFactory)
    teacher = factory.SubFactory(TeacherProfileFactory)
    room = factory.SubFactory(RoomFactory)
    group_name = factory.Sequence(lambda n: f"Группа {n}")
    max_capacity = 6
    is_active = True


class ScheduleMaskFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ScheduleMask

    schedule = factory.SubFactory(ScheduleFactory)
    target_date = factory.LazyFunction(datetime.date.today)
    type = MaskType.CANCELLATION

    class Params:
        reschedule = factory.Trait(
            type=MaskType.RESCHEDULE,
            new_start_time=datetime.time(hour=18),
            new_end_time=datetime.time(hour=19),
        )


class ParentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PARENT_MODEL

    full_name = factory.Sequence(lambda n: f"Родитель {n} Тестовый")
    phone = factory.Sequence(lambda n: f"+79{n:09d}")
    email = factory.Sequence(lambda n: f"parent{n}@example.com")


class StudentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = STUDENT_MODEL

    parent = factory.SubFactory(ParentFactory)
    full_name = factory.Sequence(lambda n: f"Ребёнок {n} Тестовый")
    dob = datetime.date(2016, 9, 1)
    school_grade = "3"


class SubscriptionPlanFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "billing.SubscriptionPlan"

    name = "Test Plan"
    slots_count = 4
    price = 4000
    base_session_price = 1000


class SubscriptionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "billing.Subscription"

    parent = factory.SubFactory(ParentFactory)
    plan = factory.SubFactory(SubscriptionPlanFactory)
    purchase_price = 4000
    base_session_price = 1000


class EnrollmentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ENROLLMENT_MODEL

    student = factory.SubFactory(StudentFactory)
    subscription = factory.SubFactory(SubscriptionFactory)
    schedule = factory.SubFactory(ScheduleFactory)
    status = "ENROLLED"

    @classmethod
    def _adjust_kwargs(cls, **kwargs):
        # Трансляция старого флага тестов в новый FSM-статус биллинга
        if "is_active" in kwargs:
            kwargs["status"] = "ENROLLED" if kwargs.pop("is_active") else "CANCELED"
        return kwargs
