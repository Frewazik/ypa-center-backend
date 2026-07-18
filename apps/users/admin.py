from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest

from unfold.admin import ModelAdmin, TabularInline

from apps.billing.models import Subscription
from apps.users.models import Parent, Student, TeacherProfile


class StudentInline(TabularInline):
    model = Student
    extra = 0
    fields = ("full_name", "dob", "school_grade", "health_issues")


class SubscriptionInline(TabularInline):
    model = Subscription
    extra = 0
    can_delete = False
    fields = ("id", "status", "purchase_price", "created_at", "expires_at")
    readonly_fields = fields
    show_change_link = True

    def has_add_permission(
        self, request: HttpRequest, obj: Parent | None = None
    ) -> bool:
        return False


@admin.register(Parent)
class ParentAdmin(ModelAdmin):
    list_display = ("id", "email", "full_name", "phone", "is_staff", "created_at")
    list_filter = ("is_staff", "is_active")
    # ПОЧЕМУ: search_fields обязателен — StudentAdmin ссылается сюда через
    # autocomplete, без него Django падает при рендере виджета
    search_fields = ("email", "full_name", "phone")
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at", "last_login")
    # ПОЧЕМУ: Parent — AUTH_USER_MODEL без пароля (вход по OTP), поле password
    # в форме провоцирует админа «починить» хэш руками
    exclude = ("password",)
    inlines = (StudentInline, SubscriptionInline)
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


@admin.register(TeacherProfile)
class TeacherProfileAdmin(ModelAdmin):
    list_display = ("id", "teacher_full_name", "middle_name", "position")
    search_fields = ("user__full_name", "user__email", "middle_name")
    list_select_related = ("user",)
    autocomplete_fields = ("user",)

    @admin.display(description="ФИО")
    def teacher_full_name(self, obj: TeacherProfile) -> str:
        return obj.user.full_name
