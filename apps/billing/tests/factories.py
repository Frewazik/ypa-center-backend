from __future__ import annotations

import factory
from django.utils import timezone

from apps.billing.models import Attendance, AttendanceStatus, SubscriptionSlot
from apps.schedule.tests.factories import EnrollmentFactory, SubscriptionFactory


class SubscriptionSlotFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = SubscriptionSlot

    subscription = factory.SubFactory(SubscriptionFactory)
    slot_id = factory.Sequence(lambda n: n + 1)
    granted_tokens = 4
    remaining_tokens = 4


class AttendanceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Attendance

    enrollment = factory.SubFactory(EnrollmentFactory)
    date = factory.LazyFunction(timezone.localdate)
    status = AttendanceStatus.ABSENT_OK
    token_debited = False
