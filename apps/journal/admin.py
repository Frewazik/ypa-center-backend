from __future__ import annotations

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone

from unfold.admin import ModelAdmin

from apps.journal.models import Lesson
from apps.users.models import TeacherProfile


def _teacher_profile(request: HttpRequest) -> TeacherProfile | None:
    if request.user.is_superuser:
        return None
    return getattr(request.user, "teacher_profile", None)


class LessonHorizonFilter(admin.SimpleListFilter):
    title = "Период"
    parameter_name = "horizon"

    def lookups(
        self, request: HttpRequest, model_admin: ModelAdmin
    ) -> list[tuple[str, str]]:
        return [
            ("today", "Сегодня"),
            ("past", "Прошедшие"),
            ("future", "Будущие"),
        ]

    def queryset(
        self, request: HttpRequest, queryset: QuerySet[Lesson]
    ) -> QuerySet[Lesson]:
        today = timezone.localdate()
        if self.value() == "today":
            return queryset.filter(date=today)
        if self.value() == "past":
            return queryset.filter(date__lt=today).order_by("-date")
        if self.value() == "future":
            return queryset.filter(date__gt=today).order_by("date")
        return queryset


@admin.register(Lesson)
class LessonAdmin(ModelAdmin):
    list_display = ("date", "schedule", "topic")
    list_editable = ("topic",)
    list_filter = (LessonHorizonFilter, "schedule")
    date_hierarchy = "date"
    search_fields = ("topic", "schedule__group_name", "schedule__activity__name")
    autocomplete_fields = ("schedule",)

    def get_queryset(self, request: HttpRequest) -> QuerySet[Lesson]:
        queryset = super().get_queryset(request).select_related("schedule__activity")
        teacher = _teacher_profile(request)
        if teacher is None:
            return queryset
        return queryset.filter(schedule__teacher=teacher)
