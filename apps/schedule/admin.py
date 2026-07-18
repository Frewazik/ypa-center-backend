# Почему: ScheduleMask намеренно не регистрируется: маски создаются только через
# create_schedule_mask (валидация коллизий + advisory-локи), прямое
# создание в админке обошло бы сервис

from __future__ import annotations

from django.contrib import admin

from unfold.admin import ModelAdmin

from apps.schedule.models import Room, Schedule, TimeSlot


@admin.register(Room)
class RoomAdmin(ModelAdmin):
    list_display = ("name", "is_active")
    list_editable = ("is_active",)
    search_fields = ("name",)


@admin.register(TimeSlot)
class TimeSlotAdmin(ModelAdmin):
    list_display = ("id", "day_of_week", "start_time", "end_time")
    list_filter = ("day_of_week",)
    ordering = ("day_of_week", "start_time")


@admin.register(Schedule)
class ScheduleAdmin(ModelAdmin):
    list_display = (
        "group_name",
        "activity",
        "teacher",
        "room",
        "day_of_week",
        "start_time",
        "end_time",
        "age_min",
        "age_max",
        "max_capacity",
        "is_active",
    )
    list_editable = ("age_min", "age_max", "max_capacity", "is_active")
    list_filter = ("is_active", "day_of_week")
    search_fields = ("group_name", "activity__name")
    list_select_related = ("activity", "teacher__user", "room", "time_slot")
    autocomplete_fields = ("activity", "teacher", "room")
