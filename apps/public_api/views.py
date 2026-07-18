from __future__ import annotations

import datetime
from typing import Final

from django.db.models import Count, Prefetch, Q, QuerySet
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import generics
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from apps.billing.models import EnrollmentStatus, SubscriptionPlan
from apps.catalog.models import Activity
from apps.content.models import GalleryImage
from apps.core.caching import cached_payload, payload_cache_key
from apps.events.models import Event
from apps.public_api.serializers import (
    ActivityCardSerializer,
    ActivityDetailSerializer,
    EventPublicSerializer,
    GalleryImagePublicSerializer,
    SubscriptionPlanPublicSerializer,
    TeacherPublicSerializer,
)
from apps.schedule.models import Schedule
from apps.users.models import TeacherProfile

POPULAR_ACTIVITIES_LIMIT: Final[int] = 3
PAST_EVENTS_VISIBILITY_DAYS: Final[int] = 7
CACHE_TTL_SHOWCASE_SECONDS: Final[int] = 60 * 5
CACHE_TTL_EVENTS_SECONDS: Final[int] = 60


def _seat_holding_filter() -> Q:
    # ПОЧЕМУ: HELD тоже держит место (Enrollment.occupies_seat) — считать
    # только ENROLLED значит показать свободным место в неоплаченной брони
    return Q(enrollment__status__in=(EnrollmentStatus.HELD, EnrollmentStatus.ENROLLED))


def _active_groups_queryset() -> QuerySet[Schedule]:
    # ПОЧЕМУ: day_of_week/start_time денормализованы триггером на schedule —
    # JOIN к time_slot не нужен. teacher__user поднимается сразу: из этих же
    # строк сериализатор собирает преподавателей кружка без новых запросов
    return (
        Schedule.objects.filter(is_active=True)
        .select_related("teacher__user")
        # ПОЧЕМУ ignore: capacity_taken объявлен на модели ради типизации
        # сериализаторов; django-stubs считает annotate() переопределением
        .annotate(  # type: ignore[no-redef]
            capacity_taken=Count("enrollment", filter=_seat_holding_filter())
        )
        .order_by("day_of_week", "start_time")
    )


@extend_schema(
    operation_id="public_activities_popular",
    summary="Топ-3 популярных кружка",
    responses=ActivityCardSerializer(many=True),
)
class PopularActivitiesView(generics.ListAPIView[Activity]):
    permission_classes = (AllowAny,)
    authentication_classes = ()
    serializer_class = ActivityCardSerializer
    pagination_class = None

    def get_queryset(self) -> QuerySet[Activity]:
        # ПОЧЕМУ: distinct обязателен — JOIN через две M2O-связи
        # размножает строки и завышает Count
        return (
            Activity.objects.filter(is_active=True)
            .annotate(
                enrollments_count=Count(
                    "slots__enrollment",
                    filter=Q(slots__enrollment__status=EnrollmentStatus.ENROLLED),
                    distinct=True,
                )
            )
            .order_by("-enrollments_count", "name")[:POPULAR_ACTIVITIES_LIMIT]
        )

    def get(self, request: Request, *args: object, **kwargs: object) -> Response:
        return cached_payload(
            key=payload_cache_key("activities_popular", request),
            ttl_seconds=CACHE_TTL_SHOWCASE_SECONDS,
            produce=lambda: super(PopularActivitiesView, self).get(
                request, *args, **kwargs
            ),
        )


@extend_schema(
    operation_id="public_activities_list",
    summary="Полный каталог кружков («Все кружки»)",
    responses=ActivityDetailSerializer(many=True),
)
class PublicActivityListView(generics.ListAPIView[Activity]):
    permission_classes = (AllowAny,)
    authentication_classes = ()
    serializer_class = ActivityDetailSerializer
    pagination_class = None

    def get_queryset(self) -> QuerySet[Activity]:
        return (
            Activity.objects.filter(is_active=True)
            .prefetch_related(Prefetch("slots", queryset=_active_groups_queryset()))
            .order_by("name")
        )

    def get(self, request: Request, *args: object, **kwargs: object) -> Response:
        return cached_payload(
            key=payload_cache_key("activities", request),
            ttl_seconds=CACHE_TTL_SHOWCASE_SECONDS,
            produce=lambda: super(PublicActivityListView, self).get(
                request, *args, **kwargs
            ),
        )


@extend_schema(
    operation_id="public_activity_detail",
    summary="Детальная карточка кружка с подгруппами",
    responses=ActivityDetailSerializer,
)
class ActivityDetailView(generics.RetrieveAPIView[Activity]):
    permission_classes = (AllowAny,)
    authentication_classes = ()
    serializer_class = ActivityDetailSerializer

    def get_queryset(self) -> QuerySet[Activity]:
        # Бюджет: 2 SQL-запроса (activity + prefetch групп с агрегатом мест)
        return Activity.objects.filter(is_active=True).prefetch_related(
            Prefetch("slots", queryset=_active_groups_queryset())
        )

    def get(self, request: Request, *args: object, **kwargs: object) -> Response:
        return cached_payload(
            key=payload_cache_key("activity_detail", request),
            ttl_seconds=CACHE_TTL_SHOWCASE_SECONDS,
            produce=lambda: super(ActivityDetailView, self).get(
                request, *args, **kwargs
            ),
        )


@extend_schema(
    operation_id="public_teachers_list",
    summary="Преподаватели с их кружками",
    responses=TeacherPublicSerializer(many=True),
)
class PublicTeacherListView(generics.ListAPIView[TeacherProfile]):
    permission_classes = (AllowAny,)
    authentication_classes = ()
    serializer_class = TeacherPublicSerializer
    pagination_class = None

    def get_queryset(self) -> QuerySet[TeacherProfile]:
        # ПОЧЕМУ: уникализацию кружков делает DISTINCT ON в БД. Строго по
        # паре (teacher_id, activity_id): prefetch выполняется одним запросом
        # на весь батч преподавателей, DISTINCT ON (activity_id) схлопнул бы
        # общий кружок двух преподавателей
        unique_activity_groups = (
            Schedule.objects.filter(is_active=True)
            .select_related("activity")
            .order_by("teacher_id", "activity_id")
            .distinct("teacher_id", "activity_id")
        )
        return (
            TeacherProfile.objects.filter(schedules__is_active=True)
            .distinct()
            .select_related("user")
            .prefetch_related(Prefetch("schedules", queryset=unique_activity_groups))
            .order_by("user__full_name")
        )

    def get(self, request: Request, *args: object, **kwargs: object) -> Response:
        return cached_payload(
            key=payload_cache_key("teachers", request),
            ttl_seconds=CACHE_TTL_SHOWCASE_SECONDS,
            produce=lambda: super(PublicTeacherListView, self).get(
                request, *args, **kwargs
            ),
        )


@extend_schema(
    operation_id="public_gallery_list",
    summary="Опубликованные фото галереи",
    responses=GalleryImagePublicSerializer(many=True),
)
class PublicGalleryListView(generics.ListAPIView[GalleryImage]):
    permission_classes = (AllowAny,)
    authentication_classes = ()
    serializer_class = GalleryImagePublicSerializer
    pagination_class = None
    queryset = GalleryImage.objects.filter(is_published=True)

    def get(self, request: Request, *args: object, **kwargs: object) -> Response:
        return cached_payload(
            key=payload_cache_key("gallery", request),
            ttl_seconds=CACHE_TTL_SHOWCASE_SECONDS,
            produce=lambda: super(PublicGalleryListView, self).get(
                request, *args, **kwargs
            ),
        )


@extend_schema(
    operation_id="public_plans_list",
    summary="Тарифные планы абонементов",
    responses=SubscriptionPlanPublicSerializer(many=True),
)
class PublicPlanListView(generics.ListAPIView[SubscriptionPlan]):
    permission_classes = (AllowAny,)
    authentication_classes = ()
    serializer_class = SubscriptionPlanPublicSerializer
    pagination_class = None
    # Безлимит уходит в конец витрины независимо от числа слотов
    queryset = SubscriptionPlan.objects.filter(is_active=True).order_by(
        "is_unlimited", "slots_count"
    )

    def get(self, request: Request, *args: object, **kwargs: object) -> Response:
        return cached_payload(
            key=payload_cache_key("plans", request),
            ttl_seconds=CACHE_TTL_SHOWCASE_SECONDS,
            produce=lambda: super(PublicPlanListView, self).get(
                request, *args, **kwargs
            ),
        )


@extend_schema(
    operation_id="public_events_list",
    summary="Афиша: будущие события и прошедшие за 7 дней",
    responses=EventPublicSerializer(many=True),
)
class PublicEventListView(generics.ListAPIView[Event]):
    permission_classes = (AllowAny,)
    authentication_classes = ()
    serializer_class = EventPublicSerializer
    pagination_class = None

    def get_queryset(self) -> QuerySet[Event]:
        # ПОЧЕМУ: граница вычисляется на каждый запрос — атрибут класса
        # заморозил бы timezone.now() на момент импорта модуля
        visibility_border = timezone.now() - datetime.timedelta(
            days=PAST_EVENTS_VISIBILITY_DAYS
        )
        return Event.objects.filter(
            is_published=True,
            start_datetime__gte=visibility_border,
        ).order_by("start_datetime")

    def get(self, request: Request, *args: object, **kwargs: object) -> Response:
        return cached_payload(
            key=payload_cache_key("events", request),
            ttl_seconds=CACHE_TTL_EVENTS_SECONDS,
            produce=lambda: super(PublicEventListView, self).get(
                request, *args, **kwargs
            ),
        )
