# ПОЧЕМУ: TypedDict типизируют возвраты для mypy strict.
# *NestedSerializer объявлены исключительно ради генерации OpenAPI-схемы (drf-spectacular),
# в рантайме данные собирает SerializerMethodField

from __future__ import annotations

from typing import TypedDict

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.schedule.services import WeekSlot

TIME_FORMAT = "%H:%M"


class ActivityPayload(TypedDict):
    id: int
    name: str
    slug: str


class PersonPayload(TypedDict):
    id: int
    full_name: str


class RoomPayload(TypedDict):
    id: int
    name: str


class CapacityPayload(TypedDict):
    max: int
    taken: int
    free: int


class OverridePayload(TypedDict):
    original_start_time: str
    reason: str


class ActivityNestedSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    slug = serializers.SlugField(read_only=True)


class TeacherNestedSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    full_name = serializers.CharField(read_only=True)


class RoomNestedSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)


class CapacityNestedSerializer(serializers.Serializer):
    max = serializers.IntegerField(read_only=True, min_value=0)
    taken = serializers.IntegerField(read_only=True, min_value=0)
    free = serializers.IntegerField(read_only=True, min_value=0)


class OverrideNestedSerializer(serializers.Serializer):
    original_start_time = serializers.CharField(read_only=True)
    reason = serializers.CharField(read_only=True)


class WeekSlotSerializer(serializers.Serializer):
    schedule_id = serializers.IntegerField(read_only=True)
    date = serializers.DateField(read_only=True)
    day_of_week = serializers.IntegerField(read_only=True)
    start_time = serializers.TimeField(read_only=True, format=TIME_FORMAT)
    end_time = serializers.TimeField(read_only=True, format=TIME_FORMAT)
    activity = serializers.SerializerMethodField()
    teacher = serializers.SerializerMethodField()
    room = serializers.SerializerMethodField()
    capacity = serializers.SerializerMethodField()
    is_rescheduled = serializers.BooleanField(read_only=True)
    is_cancelled = serializers.BooleanField(read_only=True)
    override = serializers.SerializerMethodField()

    @extend_schema_field(ActivityNestedSerializer)
    def get_activity(self, slot: WeekSlot) -> ActivityPayload:
        return {
            "id": slot.activity_id,
            "name": slot.activity_name,
            "slug": slot.activity_slug,
        }

    @extend_schema_field(TeacherNestedSerializer(allow_null=True))
    def get_teacher(self, slot: WeekSlot) -> PersonPayload | None:
        if slot.teacher_id is None or slot.teacher_full_name is None:
            return None
        return {"id": slot.teacher_id, "full_name": slot.teacher_full_name}

    @extend_schema_field(RoomNestedSerializer(allow_null=True))
    def get_room(self, slot: WeekSlot) -> RoomPayload | None:
        if slot.room_id is None or slot.room_name is None:
            return None
        return {"id": slot.room_id, "name": slot.room_name}

    @extend_schema_field(CapacityNestedSerializer)
    def get_capacity(self, slot: WeekSlot) -> CapacityPayload:
        return {
            "max": slot.capacity_max,
            "taken": slot.capacity_taken,
            "free": slot.capacity_free,
        }

    @extend_schema_field(OverrideNestedSerializer(allow_null=True))
    def get_override(self, slot: WeekSlot) -> OverridePayload | None:
        if slot.override is None:
            return None
        return {
            "original_start_time": slot.override.original_start_time.strftime(
                TIME_FORMAT
            ),
            "reason": slot.override.reason,
        }


class WeekGridResponseSerializer(serializers.Serializer):
    week_start = serializers.DateField(read_only=True)
    week_end = serializers.DateField(read_only=True)
    slots = WeekSlotSerializer(many=True, read_only=True)
