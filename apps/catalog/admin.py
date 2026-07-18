from __future__ import annotations

from django.contrib import admin

from unfold.admin import ModelAdmin

from apps.catalog.models import Activity


@admin.register(Activity)
class ActivityAdmin(ModelAdmin):
    list_display = ("name", "slug", "category", "price", "is_active")
    list_editable = ("price", "is_active")
    list_filter = ("is_active", "category")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
