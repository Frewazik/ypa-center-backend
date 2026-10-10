from __future__ import annotations

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)

from unfold.admin import ModelAdmin, TabularInline

from apps.billing.models import Subscription
from apps.core.admin import rubles_column
from apps.users.models import Parent, PersonalDataConsent, Student, TeacherProfile


class StudentInline(TabularInline):
    model = Student
    extra = 0
    # ПОЧЕМУ archived_at редактируемый: восстановить удалённого родителем
    # ребёнка можно только здесь — очистить поле (решение 2026-09-26)
    fields = ("full_name", "dob", "school_grade", "health_issues", "archived_at")


class SubscriptionInline(TabularInline):
    model = Subscription
    extra = 0
    can_delete = False
    fields = (
        "id",
        "status",
        rubles_column("purchase_price", "Цена покупки"),
        "created_at",
        "expires_at",
    )
    readonly_fields = fields
    show_change_link = True

    def has_add_permission(
        self, request: HttpRequest, obj: Parent | None = None
    ) -> bool:
        return False


# ПОЧЕМУ: служебные таблицы отзыва JWT — менеджеру в них делать нечего,
# а удаление строки из чёрного списка снова оживляет отозванный токен
admin.site.unregister(BlacklistedToken)
admin.site.unregister(OutstandingToken)


@admin.register(Parent)
class ParentAdmin(ModelAdmin):
    list_display = ("id", "email", "full_name", "phone", "is_staff", "created_at")
    list_filter = ("is_staff", "is_active", "referral_source")
    # ПОЧЕМУ: search_fields обязателен — StudentAdmin ссылается сюда через
    # autocomplete, без него Django падает при рендере виджета
    search_fields = ("email", "full_name", "phone")
    search_help_text = "Email, ФИО или телефон"
    ordering = ("-created_at",)
    # ПОЧЕМУ: согласие даёт только сам родитель на сайте — поставить его
    # «за клиента» из админки нельзя, это подделка доказательства
    readonly_fields = ("created_at", "updated_at", "last_login", "pd_consent_at")
    # ПОЧЕМУ: Parent — AUTH_USER_MODEL без пароля (вход по OTP), поле password
    # в форме провоцирует админа «починить» хэш руками
    exclude = ("password",)
    inlines = (StudentInline, SubscriptionInline)
    fieldsets = (
        (
            None,
            {"fields": ("email", "full_name", "phone", "referral_source", "comments")},
        ),
        ("Персональные данные", {"fields": ("pd_consent_at",)}),
        ("Доступ", {"fields": ("is_active", "is_staff", "is_superuser", "groups")}),
        ("Служебное", {"fields": ("last_login", "created_at", "updated_at")}),
    )


@admin.register(Student)
class StudentAdmin(ModelAdmin):
    list_display = ("id", "full_name", "school_grade", "parent", "dob", "archived_at")
    # ПОЧЕМУ архивных не прячем: это история учёта; фильтр — чтобы отделить
    list_filter = (
        ("archived_at", admin.EmptyFieldListFilter),
        ("school_grade", admin.AllValuesFieldListFilter),
    )
    list_select_related = ("parent",)
    # ПОЧЕМУ: поиск по контактам родителя — CRM-сценарий
    # «найти ребёнка по телефону/почте из заявки»
    search_fields = (
        "full_name",
        "parent__full_name",
        "parent__email",
        "parent__phone",
    )
    search_help_text = "ФИО ребёнка, email или телефон родителя"
    autocomplete_fields = ("parent",)


@admin.register(PersonalDataConsent)
class PersonalDataConsentAdmin(ModelAdmin):
    # ПОЧЕМУ: журнал — доказательство согласий по 152-ФЗ. Только чтение:
    # правка или удаление строки уничтожает доказательство
    list_display = (
        "created_at",
        "purpose",
        "document_version",
        "parent",
        "email",
        "phone",
    )
    list_filter = ("purpose", "document_version")
    search_fields = ("email", "phone", "parent__email")
    search_help_text = "Email или телефон"
    list_select_related = ("parent",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: PersonalDataConsent | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: PersonalDataConsent | None = None
    ) -> bool:
        return False


@admin.register(TeacherProfile)
class TeacherProfileAdmin(ModelAdmin):
    list_display = ("id", "teacher_full_name", "middle_name", "position")
    search_fields = ("user__full_name", "user__email", "middle_name")
    search_help_text = "ФИО или email"
    list_select_related = ("user",)
    autocomplete_fields = ("user",)

    def get_queryset(self, request: HttpRequest) -> QuerySet[TeacherProfile]:
        # ПОЧЕМУ: __str__ преподавателя читает user — без JOIN выпадающий
        # список автокомплита в расписании делал бы запрос на каждую строку
        return super().get_queryset(request).select_related("user")

    @admin.display(description="ФИО")
    def teacher_full_name(self, obj: TeacherProfile) -> str:
        return obj.user.full_name
