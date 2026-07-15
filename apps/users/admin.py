from __future__ import annotations

from django.contrib import admin

from unfold.admin import ModelAdmin

from apps.users.models import Parent, Student


@admin.register(Parent)
class ParentAdmin(ModelAdmin):
    list_display = ("id", "email", "full_name", "phone", "is_staff", "created_at")
    list_filter = ("is_staff", "is_active")
    # ПОЧЕМУ: search_fields обязателен — StudentAdmin ссылается сюда
    # через autocomplete, без него Django падает при рендере виджета
    search_fields = ("email", "full_name", "phone")
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at", "last_login")
    # ПОЧЕМУ: Parent — AUTH_USER_MODEL без пароля (вход по OTP), поле password
    # в форме провоцирует админа «починить» хэш руками
    exclude = ("password",)
    fieldsets = (
        (None, {"fields": ("email", "full_name", "phone", "comments")}),
        ("Доступ", {"fields": ("is_active", "is_staff", "is_superuser", "groups")}),
        ("Служебное", {"fields": ("last_login", "created_at", "updated_at")}),
    )


@admin.register(Student)
class StudentAdmin(ModelAdmin):
    list_display = ("id", "full_name", "school_grade", "parent", "dob")
    list_filter = (("school_grade", admin.AllValuesFieldListFilter),)
    list_select_related = ("parent",)
    # ПОЧЕМУ: поиск по контактам родителя — CRM-сценарий
    # «найти ребёнка по телефону/почте из заявки»
    search_fields = (
        "full_name",
        "parent__full_name",
        "parent__email",
        "parent__phone",
    )
    autocomplete_fields = ("parent",)
