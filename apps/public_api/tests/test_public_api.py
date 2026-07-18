from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.content.tests.factories import GalleryImageFactory
from apps.events.tests.factories import EventFactory
from apps.schedule.tests.factories import (
    ActivityFactory,
    EnrollmentFactory,
    ScheduleFactory,
    SubscriptionPlanFactory,
    TeacherProfileFactory,
    UserFactory,
)

if TYPE_CHECKING:
    from pytest_django import DjangoAssertNumQueries

pytestmark = pytest.mark.django_db

POPULAR_URL = "/api/v1/public/activities/popular/"
TEACHERS_URL = "/api/v1/public/teachers/"
GALLERY_URL = "/api/v1/public/gallery/"
EVENTS_URL = "/api/v1/public/events/"
PLANS_URL = "/api/v1/public/plans/"
CATALOG_URL = "/api/v1/public/activities/"


def _detail_url(activity_id: int) -> str:
    return f"/api/v1/public/activities/{activity_id}/"


@pytest.fixture(autouse=True)
def _isolated_cache() -> Iterator[None]:
    # !!!: изолированный LocMemCache гарантирует, что payload-кэш вьюх
    # не протечет между независимыми тестами и параллельными xdist-воркерами
    with override_settings(
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": f"public-api-{uuid.uuid4()}",
            }
        }
    ):
        yield


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


class TestPopularActivities:
    def test_returns_top_three_by_enrollments(self, api_client: APIClient) -> None:
        leader = ActivityFactory(name="Шахматы")
        runner_up = ActivityFactory(name="Робототехника")
        outsider = ActivityFactory(name="Лепка")
        ActivityFactory(name="Четвёртый кружок")
        inactive = ActivityFactory(name="Закрытый", is_active=False)

        leader_group = ScheduleFactory(activity=leader)
        EnrollmentFactory.create_batch(3, schedule=leader_group)
        EnrollmentFactory(schedule=ScheduleFactory(activity=runner_up))
        EnrollmentFactory(schedule=ScheduleFactory(activity=outsider), is_active=False)
        EnrollmentFactory.create_batch(5, schedule=ScheduleFactory(activity=inactive))

        response = api_client.get(POPULAR_URL)

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert len(payload) == 3
        assert payload[0]["name"] == "Шахматы"
        assert payload[1]["name"] == "Робототехника"
        assert "Закрытый" not in {item["name"] for item in payload}

    def test_card_contains_showcase_fields(self, api_client: APIClient) -> None:
        ActivityFactory(
            name="Шахматы",
            cover_image="https://cdn.example.com/chess.jpg",
            short_description="Учим думать на три хода вперёд",
            features=["Развитие логики", "Память"],
            tags=["младшие", "старшие"],
        )

        response = api_client.get(POPULAR_URL)

        card = response.json()[0]
        assert card["cover_image"] == "https://cdn.example.com/chess.jpg"
        assert card["short_description"] == "Учим думать на три хода вперёд"
        assert card["features"] == ["Развитие логики", "Память"]
        assert card["tags"] == ["младшие", "старшие"]


class TestActivityDetail:
    def test_returns_active_groups_with_age_and_seats(
        self, api_client: APIClient
    ) -> None:
        activity = ActivityFactory()
        group = ScheduleFactory(
            activity=activity, max_capacity=6, age_min=7, age_max=10
        )
        ScheduleFactory(activity=activity, is_active=False)
        EnrollmentFactory.create_batch(2, schedule=group)
        EnrollmentFactory(schedule=group, is_active=False)

        response = api_client.get(_detail_url(activity.pk))

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert len(payload["groups"]) == 1
        group_payload = payload["groups"][0]
        assert group_payload["age_min"] == 7
        assert group_payload["age_max"] == 10
        assert group_payload["max_capacity"] == 6
        assert group_payload["seats_free"] == 4
        assert group_payload["start_time"] == group.start_time.strftime("%H:%M")

    def test_query_budget_is_two(
        self,
        api_client: APIClient,
        django_assert_num_queries: DjangoAssertNumQueries,
    ) -> None:
        activity = ActivityFactory()
        for _ in range(4):
            group = ScheduleFactory(activity=activity)
            EnrollmentFactory(schedule=group)

        with django_assert_num_queries(2):
            response = api_client.get(_detail_url(activity.pk))

        assert response.status_code == status.HTTP_200_OK
        assert len(response.json()["groups"]) == 4

    def test_inactive_activity_returns_404(self, api_client: APIClient) -> None:
        activity = ActivityFactory(is_active=False)

        response = api_client.get(_detail_url(activity.pk))

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestPublicTeachers:
    def test_lists_teachers_with_unique_activities(self, api_client: APIClient) -> None:
        teacher = TeacherProfileFactory(user=UserFactory(full_name="Анна Иванова"))
        chess = ActivityFactory(name="Шахматы")
        ScheduleFactory(teacher=teacher, activity=chess)
        ScheduleFactory(teacher=teacher, activity=chess)
        ScheduleFactory(teacher=teacher, activity=ActivityFactory(name="Робототехника"))

        # ПОЧЕМУ: преподаватели без назначенных групп исключаются из выдачи на витрине
        TeacherProfileFactory()

        response = api_client.get(TEACHERS_URL)

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]["full_name"] == "Анна Иванова"
        names = [activity["name"] for activity in payload[0]["activities"]]
        assert sorted(names) == ["Робототехника", "Шахматы"]

    def test_shared_activity_not_collapsed_between_teachers(
        self, api_client: APIClient
    ) -> None:
        # ПОЧЕМУ: регресс-тест на правильное использование DISTINCT ON (teacher_id, activity_id)
        # без него общая дисциплина схлопывается и выводится только у одного преподавателя
        chess = ActivityFactory(name="Шахматы")
        first = TeacherProfileFactory(user=UserFactory(full_name="Анна"))
        second = TeacherProfileFactory(user=UserFactory(full_name="Борис"))
        ScheduleFactory(teacher=first, activity=chess)
        ScheduleFactory(teacher=second, activity=chess)

        response = api_client.get(TEACHERS_URL)

        payload = response.json()
        assert len(payload) == 2
        for teacher_payload in payload:
            names = [activity["name"] for activity in teacher_payload["activities"]]
            assert names == ["Шахматы"]

    def test_query_budget_is_two(
        self,
        api_client: APIClient,
        django_assert_num_queries: DjangoAssertNumQueries,
    ) -> None:
        for _ in range(3):
            ScheduleFactory()

        with django_assert_num_queries(2):
            response = api_client.get(TEACHERS_URL)

        assert response.status_code == status.HTTP_200_OK
        assert len(response.json()) == 3


class TestPublicGallery:
    def test_returns_only_published_ordered(self, api_client: APIClient) -> None:
        GalleryImageFactory(order=2, image_url="https://cdn.example.com/2.jpg")
        GalleryImageFactory(order=0, image_url="https://cdn.example.com/0.jpg")
        GalleryImageFactory(order=1, image_url="https://cdn.example.com/1.jpg")
        GalleryImageFactory(is_published=False)

        response = api_client.get(GALLERY_URL)

        assert response.status_code == status.HTTP_200_OK
        urls = [item["image_url"] for item in response.json()]
        assert urls == [
            "https://cdn.example.com/0.jpg",
            "https://cdn.example.com/1.jpg",
            "https://cdn.example.com/2.jpg",
        ]

    def test_second_request_is_served_from_payload_cache(
        self,
        api_client: APIClient,
        django_assert_num_queries: DjangoAssertNumQueries,
    ) -> None:
        GalleryImageFactory()
        first = api_client.get(GALLERY_URL)

        with django_assert_num_queries(0):
            second = api_client.get(GALLERY_URL)

        assert second.status_code == status.HTTP_200_OK
        assert second.json() == first.json()

    def test_cache_key_ignores_query_string(
        self,
        api_client: APIClient,
        django_assert_num_queries: DjangoAssertNumQueries,
    ) -> None:
        # !!!: защита от cache poisoning
        # случайные GET-параметры не должны плодить ключи в кэше и пробивать запросы до БД
        GalleryImageFactory()
        first = api_client.get(GALLERY_URL)

        with django_assert_num_queries(0):
            poisoned = api_client.get(f"{GALLERY_URL}?rnd=12345")

        assert poisoned.status_code == status.HTTP_200_OK
        assert poisoned.json() == first.json()


class TestPublicEvents:
    def test_visibility_window(self, api_client: APIClient) -> None:
        now = timezone.now()
        recent_past = EventFactory(
            title="Прошедшее недавно",
            start_datetime=now - datetime.timedelta(days=3),
        )
        upcoming = EventFactory(
            title="Будущее", start_datetime=now + datetime.timedelta(days=3)
        )
        EventFactory(title="Старое", start_datetime=now - datetime.timedelta(days=10))
        EventFactory(
            title="Черновик",
            start_datetime=now + datetime.timedelta(days=3),
            is_published=False,
        )

        response = api_client.get(EVENTS_URL)

        assert response.status_code == status.HTTP_200_OK
        titles = [item["title"] for item in response.json()]
        assert titles == [recent_past.title, upcoming.title]

    def test_event_payload_shape(self, api_client: APIClient) -> None:
        event = EventFactory(paid=True)

        response = api_client.get(EVENTS_URL)

        payload = response.json()[0]
        assert payload["id"] == event.pk
        assert payload["price"] == 150_000
        assert payload["is_free"] is False
        assert payload["is_upcoming"] is True
        assert payload["capacity"] == 20


class TestPublicPlans:
    def test_lists_active_plans_unlimited_last(self, api_client: APIClient) -> None:
        SubscriptionPlanFactory(
            name="Безлимит", slots_count=0, price=1_500_000, is_unlimited=True
        )
        SubscriptionPlanFactory(name="4 занятия", slots_count=4, price=400_000)
        SubscriptionPlanFactory(name="Скрытый", is_active=False)

        response = api_client.get(PLANS_URL)

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert [plan["name"] for plan in payload] == ["4 занятия", "Безлимит"]
        assert payload[0]["price_per_session"] == 100_000
        assert payload[1]["price_per_session"] is None

    def test_price_change_is_visible_immediately(self, api_client: APIClient) -> None:
        # !!!: регресс-тест инвалидации кэша по сигналу от БД
        # правка сущности обязана отобразиться на витрине мгновенно, до истечения базового TTL
        plan = SubscriptionPlanFactory(slots_count=4, price=400_000)
        assert api_client.get(PLANS_URL).json()[0]["price"] == 400_000

        plan.price = 350_000
        plan.save()

        payload = api_client.get(PLANS_URL).json()[0]
        assert payload["price"] == 350_000
        assert payload["price_per_session"] == 87_500


class TestShowcaseCacheInvalidation:
    def test_activity_rename_is_visible_immediately(
        self, api_client: APIClient
    ) -> None:
        activity = ActivityFactory(name="Шахматы")
        assert api_client.get(POPULAR_URL).json()[0]["name"] == "Шахматы"

        activity.name = "Шахматы PRO"
        activity.save()

        assert api_client.get(POPULAR_URL).json()[0]["name"] == "Шахматы PRO"

    def test_gallery_publish_is_visible_immediately(
        self, api_client: APIClient
    ) -> None:
        image = GalleryImageFactory(is_published=False)
        assert api_client.get(GALLERY_URL).json() == []

        image.is_published = True
        image.save()

        assert len(api_client.get(GALLERY_URL).json()) == 1


class TestPublicActivitiesCatalog:
    def test_catalog_card_has_description_teachers_and_days(
        self, api_client: APIClient
    ) -> None:
        activity = ActivityFactory(
            name="Английский язык",
            description="Погружение в язык через живое общение.",
        )
        teacher = TeacherProfileFactory(
            user=UserFactory(full_name="Надежда Геннадьевна Макуха"),
            photo_url="https://cdn.example.com/teachers/makukha.jpg",
            position="Учитель Английского Языка",
        )
        ScheduleFactory(activity=activity, teacher=teacher)
        ScheduleFactory(activity=activity, teacher=teacher)
        ScheduleFactory(activity=activity, is_active=False)

        response = api_client.get(CATALOG_URL)

        assert response.status_code == status.HTTP_200_OK
        card = response.json()[0]
        assert card["description"] == "Погружение в язык через живое общение."
        assert len(card["groups"]) == 2
        assert len(card["teachers"]) == 1
        assert card["teachers"][0]["full_name"] == "Надежда Геннадьевна Макуха"
        assert card["teachers"][0]["position"] == "Учитель Английского Языка"
        assert card["days_of_week"] == sorted(
            {group["day_of_week"] for group in card["groups"]}
        )

    def test_inactive_activities_hidden(self, api_client: APIClient) -> None:
        ActivityFactory(is_active=False)

        response = api_client.get(CATALOG_URL)

        assert response.json() == []

    def test_query_budget_is_two(
        self,
        api_client: APIClient,
        django_assert_num_queries: DjangoAssertNumQueries,
    ) -> None:
        for _ in range(3):
            ScheduleFactory()

        with django_assert_num_queries(2):
            response = api_client.get(CATALOG_URL)

        assert response.status_code == status.HTTP_200_OK
        assert len(response.json()) == 3


class TestTeacherShowcaseFields:
    def test_teacher_card_has_photo_position_quote_bio(
        self, api_client: APIClient
    ) -> None:
        teacher = TeacherProfileFactory(
            user=UserFactory(full_name="Яков Леонидович Мордвинов"),
            photo_url="https://cdn.example.com/teachers/mordvinov.jpg",
            position="Педагог Кружка Мышления",
            quote="Думать - это навык. Как и любой навык, его можно тренировать.",
            bio="Выпускник ФМШ и НГУ, кандидат физико-математических наук.",
        )
        ScheduleFactory(teacher=teacher)

        response = api_client.get(TEACHERS_URL)

        card = response.json()[0]
        assert card["photo_url"] == "https://cdn.example.com/teachers/mordvinov.jpg"
        assert card["position"] == "Педагог Кружка Мышления"
        assert card["quote"].startswith("Думать - это навык.")
        assert card["bio"].startswith("Выпускник ФМШ")
