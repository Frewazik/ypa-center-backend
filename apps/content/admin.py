from __future__ import annotations

from django.contrib import admin

from unfold.admin import ModelAdmin

from apps.content.models import GalleryImage


@admin.register(GalleryImage)
class GalleryImageAdmin(ModelAdmin):
    list_display = ("id", "image_url", "order", "is_published", "created_at")
    list_editable = ("order", "is_published")
    list_filter = ("is_published",)
    ordering = ("order",)
