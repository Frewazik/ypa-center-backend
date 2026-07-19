from __future__ import annotations

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from apps.billing.models import Subscription, SubscriptionPlan, SubscriptionSlot
from apps.catalog.models import Activity
from apps.events.models import Event
from apps.schedule.models import Schedule
from apps.users.models import Parent, Student

pytestmark = pytest.mark.django_db


class TestSeedDemo:
    def test_populates_showcase_and_family(self) -> None:
        call_command("seed_demo")

        assert Activity.objects.count() == 4
        assert Schedule.objects.count() == 8
        assert SubscriptionPlan.objects.count() == 6
        assert Event.objects.filter(is_published=True).count() == 2

        parent = Parent.objects.get(email="parent@demo.ru")
        assert Student.objects.filter(parent=parent).count() == 2
        subscription = Subscription.objects.get(parent=parent)
        assert SubscriptionSlot.objects.filter(subscription=subscription).count() == 2

        admin = Parent.objects.get(email="admin@demo.ru")
        assert admin.is_superuser
        assert admin.check_password("admin123")

    def test_second_run_is_idempotent(self) -> None:
        call_command("seed_demo")
        call_command("seed_demo")

        assert Activity.objects.count() == 4
        assert Schedule.objects.count() == 8
        assert SubscriptionPlan.objects.count() == 6
        assert Subscription.objects.filter(parent__email="parent@demo.ru").count() == 1

    def test_refuses_outside_debug(self) -> None:
        # ПОЧЕМУ: сиды содержат фиксированный пароль админа —
        # на проде команда обязана отказать
        with override_settings(DEBUG=False):
            with pytest.raises(CommandError):
                call_command("seed_demo")

    def test_no_admin_flag_skips_superuser(self) -> None:
        call_command("seed_demo", "--no-admin")
        assert not Parent.objects.filter(email="admin@demo.ru").exists()
