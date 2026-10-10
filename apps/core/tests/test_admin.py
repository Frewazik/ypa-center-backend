from __future__ import annotations

from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from apps.billing.models import SubscriptionPlan
from apps.core.admin import RublesField, format_rubles
from apps.schedule.tests.factories import TeacherProfileFactory


class TestFormatRubles:
    def test_whole_rubles_without_kopecks(self) -> None:
        assert format_rubles(700_000) == "7 000 ₽"

    def test_kopecks_shown_when_present(self) -> None:
        assert format_rubles(120_050) == "1 200,50 ₽"


class TestRublesField:
    def _field(self) -> RublesField:
        return RublesField(label="Цена, ₽", required=True)

    def test_rubles_input_stored_as_kopecks(self) -> None:
        assert self._field().clean("1200.50") == 120_050

    def test_kopecks_from_db_shown_as_rubles(self) -> None:
        assert self._field().prepare_value(120_000) == Decimal(1200)

    def test_same_amount_is_not_a_change(self) -> None:
        assert not self._field().has_changed(120_000, "1200")

    def test_other_amount_is_a_change(self) -> None:
        assert self._field().has_changed(120_000, "1300")


@pytest.mark.django_db
class TestRublesInAdmin:
    def test_plan_price_typed_in_rubles_saved_in_kopecks(
        self, admin_client: Client
    ) -> None:
        response = admin_client.post(
            reverse("admin:billing_subscriptionplan_add"),
            {
                "name": "4 занятия",
                "slots_count": 1,
                "price": "4000",
                "base_session_price": "1200",
                "is_active": "on",
            },
        )

        assert response.status_code == 302
        plan = SubscriptionPlan.objects.get()
        assert (plan.price, plan.base_session_price) == (400_000, 120_000)


@pytest.mark.django_db
class TestAdminLogin:
    def test_login_without_next_lands_on_admin_index(self, admin_user: object) -> None:
        response = Client().post(
            reverse("admin:login"),
            {"username": "admin@example.com", "password": "password"},
        )

        assert response.status_code == 302
        assert response["Location"] == reverse("admin:index")


@pytest.mark.django_db
def test_teacher_shown_by_full_name_with_middle_name() -> None:
    teacher = TeacherProfileFactory(
        user__full_name="Волков Игорь", middle_name="Петрович"
    )

    assert str(teacher) == "Волков Игорь Петрович"
