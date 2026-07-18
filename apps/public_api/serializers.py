from __future__ import annotations

from typing import TypedDict

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.billing.models import SubscriptionPlan
from apps.catalog.models import Activity
from apps.content.models import GalleryImage
from apps.events.models import Event
from apps.schedule.models import Schedule
from apps.users.models import TeacherProfile

TIME_FORMAT = "%H:%M"


class ActivityCardSerializer(serializers.ModelSerializer[Activity]):
    class Meta:
        model = Activity
        # ПОЧЕМУ аннотация: наследник расширяет кортеж; без явного типа mypy
        # фиксирует длину кортежа из вывода и запрещает переопределение
        fields: tuple[str, ...] = (
            "id",
            "name",
            "slug",
            "category",
            "price",
            "cover_image",
            "short_description",
            "features",
            "tags",
        )


class ScheduleGroupPublicSerializer(serializers.ModelSerializer[Schedule]):
    day_of_week_display = serializers.CharField(
        source="get_day_of_week_display", read_only=True
    )
    start_time = serializers.TimeField(read_only=True, format=TIME_FORMAT)
    end_time = serializers.TimeField(read_only=True, format=TIME_FORMAT)
    seats_free = serializers.SerializerMethodField()

    class Meta:
        model = Schedule
        fields = (
            "id",
            "group_name",
            "age_min",
            "age_max",
            "day_of_week",
            "day_of_week_display",
            "start_time",
            "end_time",
            "max_capacity",
            "seats_free",
        )

    def get_seats_free(self, schedule: Schedule) -> int:
        # ПОЧЕМУ: capacity_taken обязан приходить из annotate() вьюхи;
        # обращение к enrollment здесь породило бы N+1
        return max(schedule.max_capacity - schedule.capacity_taken, 0)


class ActivityTeacherPayload(TypedDict):
    id: int
    full_name: str
    photo_url: str
    position: str


class ActivityTeacherNestedSerializer(serializers.Serializer[ActivityTeacherPayload]):
    id = serializers.IntegerField(read_only=True)
    full_name = serializers.CharField(read_only=True)
    photo_url = serializers.URLField(read_only=True, allow_blank=True)
    position = serializers.CharField(read_only=True, allow_blank=True)


class ActivityDetailSerializer(ActivityCardSerializer):
    groups = ScheduleGroupPublicSerializer(many=True, read_only=True, source="slots")
    teachers = serializers.SerializerMethodField()
    days_of_week = serializers.SerializerMethodField()

    class Meta(ActivityCardSerializer.Meta):
        fields = (
            *ActivityCardSerializer.Meta.fields,
            "description",
            "groups",
            "teachers",
            "days_of_week",
        )

    @extend_schema_field(ActivityTeacherNestedSerializer(many=True))
    def get_teachers(self, activity: Activity) -> list[ActivityTeacherPayload]:
        unique: dict[int, ActivityTeacherPayload] = {}
        for schedule in activity.slots.all():
            teacher = schedule.teacher
            if teacher is None or teacher.pk in unique:
                continue
            unique[teacher.pk] = {
                "id": teacher.pk,
                "full_name": teacher.user.full_name,
                "photo_url": teacher.photo_url,
                "position": teacher.position,
            }
        return list(unique.values())

    @extend_schema_field(serializers.ListField(child=serializers.IntegerField()))
    def get_days_of_week(self, activity: Activity) -> list[int]:
        return sorted({schedule.day_of_week for schedule in activity.slots.all()})


class TeacherActivityPayload(TypedDict):
    id: int
    name: str
    slug: str


class TeacherActivityNestedSerializer(serializers.Serializer[TeacherActivityPayload]):
    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    slug = serializers.SlugField(read_only=True)


class TeacherPublicSerializer(serializers.ModelSerializer[TeacherProfile]):
    full_name = serializers.CharField(source="user.full_name", read_only=True)
    activities = serializers.SerializerMethodField()

    class Meta:
        model = TeacherProfile
        fields = (
            "id",
            "full_name",
            "middle_name",
            "photo_url",
            "position",
            "quote",
            "bio",
            "activities",
        )

    @extend_schema_field(TeacherActivityNestedSerializer(many=True))
    def get_activities(self, teacher: TeacherProfile) -> list[TeacherActivityPayload]:
        return [
            {
                "id": schedule.activity.pk,
                "name": schedule.activity.name,
                "slug": schedule.activity.slug,
            }
            for schedule in teacher.schedules.all()
        ]


class GalleryImagePublicSerializer(serializers.ModelSerializer[GalleryImage]):
    class Meta:
        model = GalleryImage
        fields = ("id", "image_url", "order")


class SubscriptionPlanPublicSerializer(serializers.ModelSerializer[SubscriptionPlan]):
    price_per_session = serializers.IntegerField(read_only=True)

    class Meta:
        model = SubscriptionPlan
        fields = (
            "id",
            "name",
            "slots_count",
            "price",
            "price_per_session",
            "is_unlimited",
        )


class EventPublicSerializer(serializers.ModelSerializer[Event]):
    is_free = serializers.BooleanField(read_only=True)
    is_upcoming = serializers.BooleanField(read_only=True)

    class Meta:
        model = Event
        fields = (
            "id",
            "title",
            "description",
            "cover_image",
            "start_datetime",
            "duration_minutes",
            "price",
            "is_free",
            "capacity",
            "is_upcoming",
        )
