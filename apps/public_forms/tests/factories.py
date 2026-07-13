from __future__ import annotations

import factory
from factory.django import DjangoModelFactory

from apps.public_forms.models import CallbackRequest, CallTimeWindow, FeedbackRequest


class CallbackRequestFactory(DjangoModelFactory):
    class Meta:
        model = CallbackRequest

    name = factory.Sequence(lambda n: f"Родитель {n}")
    phone = factory.Sequence(lambda n: f"+7999123{n % 10000:04d}")
    preferred_time_window = CallTimeWindow.MORNING


class FeedbackRequestFactory(DjangoModelFactory):
    class Meta:
        model = FeedbackRequest

    name = factory.Sequence(lambda n: f"Родитель {n}")
    email = factory.Sequence(lambda n: f"parent{n}@example.com")
    message = "Со скольки лет принимаете на английский язык?"