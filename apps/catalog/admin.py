from __future__ import annotations

from django.contrib import admin
from unfold.admin import ModelAdmin

from apps.catalog.models import Activity


@admin.register(Activity)
class ActivityAdmin(ModelAdmin):
    list_display = ("name", "slug", "category", "is_active")
    list_editable = ("is_active",)
    list_filter = ("is_active", "category")
    search_fields = ("name", "slug")
    search_help_text = "Название или адрес в ссылке"
    prepopulated_fields = {"slug": ("name",)}
