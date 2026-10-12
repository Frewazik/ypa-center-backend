from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from apps.catalog.models import Activity

pytestmark = pytest.mark.django_db


def test_activity_is_created_without_price(admin_client: Client) -> None:
    response = admin_client.post(
        reverse("admin:catalog_activity_add"),
        {
            "name": "Шахматы",
            "slug": "shahmaty",
            "category": "CLUB",
            "is_active": "on",
            "cover_image": "",
            "short_description": "",
            "description": "",
            "features": "[]",
            "tags": "[]",
        },
    )

    assert response.status_code == 302
    assert Activity.objects.get().name == "Шахматы"
