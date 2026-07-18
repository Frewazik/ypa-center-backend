from __future__ import annotations

import datetime

import factory
from django.db.models import F
from django.utils import timezone
from factory.django import DjangoModelFactory

from apps.events.models import Event, EventRegistration, RegistrationStatus


class EventFactory(DjangoModelFactory):
    class Meta:
        model = Event

    title = factory.Sequence(lambda n: f"Событие {n}")
    description = "Открытый мастер-класс для всей семьи."
    cover_image = factory.Sequence(lambda n: f"https://cdn.example.com/events/{n}.jpg")
    start_datetime = factory.LazyFunction(
        lambda: timezone.now() + datetime.timedelta(days=7)
    )
    duration_minutes = 90
    price = 0
    capacity = 20
    seats_taken = 0
    is_published = True

    class Params:
        past = factory.Trait(
            start_datetime=factory.LazyFunction(
                lambda: timezone.now() - datetime.timedelta(days=1)
            )
        )
        paid = factory.Trait(price=150_000)


class EventRegistrationFactory(DjangoModelFactory):
    class Meta:
        model = EventRegistration
        # ПОЧЕМУ: sync_event_seats ниже правит Event, а не сам obj - лишний
        # implicit re-save после postgeneration тут не нужен (factory_boy
        # предупреждает, что уберёт его по умолчанию в следующей мажорной)
        skip_postgeneration_save = True

    event = factory.SubFactory(EventFactory)
    child_name = factory.Sequence(lambda n: f"Ребёнок {n}")
    parent_name = factory.Sequence(lambda n: f"Родитель {n}")
    phone = factory.Sequence(lambda n: f"+7912{n % 10000000:07d}")
    email = factory.Sequence(lambda n: f"guest{n}@example.com")
    attendees_count = 1
    source = "instagram"
    status = RegistrationStatus.NEW

    @factory.post_generation
    def sync_event_seats(
        obj: EventRegistration,  # noqa: N805
        create: bool,
        extracted: object,
        **kwargs: object,
    ) -> None:
        # Почему: бронь в блокирующем статусе
        # всегда отражена в денормализованном Event.seats_taken
        if create and obj.occupies_seats:
            Event.objects.filter(pk=obj.event_id).update(
                seats_taken=F("seats_taken") + obj.attendees_count
            )
