from __future__ import annotations

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from unfold.admin import ModelAdmin

from apps.events.models import Event, EventRegistration, RegistrationStatus
from apps.events.services import cancel_registration


@admin.register(Event)
class EventAdmin(ModelAdmin):
    list_display = (
        "title",
        "start_datetime",
        "price",
        "capacity",
        "seats_taken",
        "is_published",
    )
    list_filter = ("is_published",)
    search_fields = ("title",)
    ordering = ("-start_datetime",)
    readonly_fields = ("seats_taken",)


@admin.register(EventRegistration)
class EventRegistrationAdmin(ModelAdmin):
    list_display = (
        "event",
        "child_name",
        "parent_name",
        "phone",
        "attendees_count",
        "status",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = ("child_name", "parent_name", "phone", "email")
    list_select_related = ("event",)
    # ПОЧЕМУ: status/attendees_count/event участвуют в инварианте
    # Event.seats_taken — правки только через сервисы и экшены
    readonly_fields = ("event", "attendees_count", "status")
    actions = ("confirm_selected", "cancel_selected")

    def has_add_permission(self, request: HttpRequest) -> bool:
        # ПОЧЕМУ: создание в обход register_for_event не инкрементирует
        # Event.seats_taken — регистрация только через публичный API
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: EventRegistration | None = None
    ) -> bool:
        # ПОЧЕМУ: физическое удаление не декрементирует счётчик — вместо
        # удаления экшен «Отменить и освободить места»
        return False

    @admin.action(description="Подтвердить оплату")
    def confirm_selected(
        self, request: HttpRequest, queryset: QuerySet[EventRegistration]
    ) -> None:
        queryset.filter(
            status__in=(RegistrationStatus.NEW, RegistrationStatus.PENDING_PAYMENT)
        ).update(status=RegistrationStatus.CONFIRMED)

    @admin.action(description="Отменить и освободить места")
    def cancel_selected(
        self, request: HttpRequest, queryset: QuerySet[EventRegistration]
    ) -> None:
        for registration_id in queryset.values_list("pk", flat=True):
            cancel_registration(registration_id)
