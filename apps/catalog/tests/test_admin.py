from __future__ import annotations

import pytest
from django.contrib.admin.sites import site
from django.test import Client, RequestFactory
from django.urls import reverse

from apps.catalog.admin import ActivityAdmin
from apps.catalog.models import Activity
from apps.schedule.tests.factories import ActivityFactory

pytestmark = pytest.mark.django_db

_ADD_URL = reverse("admin:catalog_activity_add")


def _form_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "name": "Шахматы",
        "slug": "shahmaty",
        "category": "CLUB",
        "price": 120_000,
        "is_active": "on",
        "cover_image": "",
        "short_description": "",
        "description": "",
        "features": "[]",
        "tags": "[]",
    }
    data.update(overrides)
    return data


def _change_url(activity: Activity) -> str:
    return reverse("admin:catalog_activity_change", args=[activity.pk])


class TestFreeTrialPriceConfirmation:
    def test_new_activity_with_zero_price_needs_confirmation(
        self, admin_client: Client
    ) -> None:
        response = admin_client.post(_ADD_URL, _form_data(price=0))

        assert response.status_code == 200
        assert "Да, пробное бесплатное" in response.content.decode()
        assert not Activity.objects.exists()

    def test_confirmed_zero_price_is_saved(self, admin_client: Client) -> None:
        response = admin_client.post(
            _ADD_URL, _form_data(price=0, confirm_free_trial="on")
        )

        assert response.status_code == 302
        assert Activity.objects.get().price == 0

    def test_paid_activity_saved_without_confirmation(
        self, admin_client: Client
    ) -> None:
        response = admin_client.post(_ADD_URL, _form_data())

        assert response.status_code == 302
        assert Activity.objects.get().price == 120_000

    def test_changing_price_to_zero_needs_confirmation(
        self, admin_client: Client
    ) -> None:
        activity = ActivityFactory(price=120_000)

        response = admin_client.post(
            _change_url(activity),
            _form_data(name=activity.name, slug=activity.slug, price=0),
        )

        assert response.status_code == 200
        activity.refresh_from_db()
        assert activity.price == 120_000

    def test_already_free_activity_edits_without_reconfirmation(
        self, admin_client: Client
    ) -> None:
        activity = ActivityFactory(price=0)

        response = admin_client.post(
            _change_url(activity),
            _form_data(name="Новое имя", slug=activity.slug, price=0),
        )

        assert response.status_code == 302
        activity.refresh_from_db()
        assert activity.name == "Новое имя"

    def test_list_row_cannot_set_zero_price(self, admin_user: object) -> None:
        activity = ActivityFactory(price=120_000)
        request = RequestFactory().get("/")
        request.user = admin_user  # type: ignore[assignment]
        form_class = ActivityAdmin(Activity, site).get_changelist_form(request)

        form = form_class(data={"price": 0, "is_active": "on"}, instance=activity)

        assert not form.is_valid()
        assert "карточке кружка" in form.errors["price"][0]
