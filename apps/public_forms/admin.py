from __future__ import annotations

from django.contrib import admin

from unfold.admin import ModelAdmin

from apps.public_forms.models import CallbackRequest, FeedbackRequest


@admin.register(CallbackRequest)
class CallbackRequestAdmin(ModelAdmin):
    list_display = ("name", "phone", "preferred_time_window", "status", "created_at")
    list_editable = ("status",)
    list_filter = ("status", "preferred_time_window")
    search_fields = ("name", "phone")


@admin.register(FeedbackRequest)
class FeedbackRequestAdmin(ModelAdmin):
    list_display = ("name", "email", "status", "created_at")
    list_editable = ("status",)
    list_filter = ("status",)
    search_fields = ("name", "email", "message")
