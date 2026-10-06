from __future__ import annotations

from collections import Counter
from typing import Final

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.forms import ModelForm
from django.http import HttpRequest

from unfold.admin import ModelAdmin

from apps.billing.ports import resolve_event_port
from apps.billing.services import BillingError, cancel_event_registration
from apps.events.models import Event, EventRegistration
from apps.events.services import (
    cancel_registration,
    confirm_registration,
    has_online_payment,
)

_EVENT_ADMIN_EDITABLE_FIELDS: Final[tuple[str, ...]] = tuple(
    field.name
    for field in Event._meta.concrete_fields
    if not field.primary_key and field.name != "seats_taken"
)


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

    def save_model(
        self, request: HttpRequest, obj: Event, form: ModelForm[Event], change: bool
    ) -> None:
        if not change:
            super().save_model(request, obj, form, change)
            return
        # ПОЧЕМУ: полный save() записал бы seats_taken, прочитанный при отправке
        # формы, поверх брони, закоммиченной за это время, — места продались бы
        # дважды. Счётчик меняют только сервисы events под локом события
        obj.save(update_fields=_EVENT_ADMIN_EDITABLE_FIELDS)


@admin.register(EventRegistration)
class EventRegistrationAdmin(ModelAdmin):
    list_display = (
        "event",
        "child_name",
        "parent_name",
        "phone",
        "attendees_count",
        "amount",
        "status",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = ("child_name", "parent_name", "phone", "email")
    list_select_related = ("event",)
    # ПОЧЕМУ: status/attendees_count/event участвуют в инварианте
    # Event.seats_taken — правки только через сервисы и экшены; amount —
    # снимок цены на момент записи, по нему ЛК и выручка
    readonly_fields = ("event", "attendees_count", "amount", "status")
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
        ids = list(queryset.values_list("pk", flat=True))
        online = {pk for pk in ids if has_online_payment(pk)}
        confirmed = sum(
            1 for pk in ids if pk not in online and confirm_registration(pk)
        )
        # ПОЧЕМУ: бронь могла сняться по TTL, пока менеджер держал страницу
        # открытой, — без сообщения он решил бы, что подтвердил её
        self.message_user(request, f"Подтверждено: {confirmed}.")
        if online:
            # ПОЧЕМУ: наличные поверх онлайн-платежа — семья заплатит дважды
            self.message_user(
                request,
                f"Пропущено: {len(online)} — оплачиваются онлайн, "
                "подтверждаются сами после оплаты.",
                level=messages.WARNING,
            )
        if skipped := len(ids) - len(online) - confirmed:
            self.message_user(
                request,
                f"Пропущено: {skipped} — уже подтверждены или отменены.",
                level=messages.WARNING,
            )

    @admin.action(description="Отменить и освободить места")
    def cancel_selected(
        self, request: HttpRequest, queryset: QuerySet[EventRegistration]
    ) -> None:
        # ПОЧЕМУ поштучно: у каждой брони своя короткая транзакция — пачка под
        # одной держала бы локи всех событий сразу
        counts: Counter[str] = Counter()
        event_port = resolve_event_port()
        for registration_id in queryset.values_list("pk", flat=True):
            if not has_online_payment(registration_id):
                counts[
                    "canceled" if cancel_registration(registration_id) else "skipped"
                ] += 1
                continue
            try:
                outcome = cancel_event_registration(
                    registration_id,
                    event_port=event_port,
                    canceled_by=request.user.get_username(),
                )
            except BillingError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)
                continue
            counts[outcome] += 1

        refunded = counts["REFUND_QUEUED"]
        self.message_user(
            request,
            f"Отменено: {counts['canceled'] + refunded}, "
            f"поставлено возвратов: {refunded}.",
        )
        if awaiting := counts["AWAITING_PAYMENT"]:
            self.message_user(
                request,
                f"Пропущено: {awaiting} — ждут онлайн-оплаты и снимутся сами "
                "примерно через 20 минут, если не оплатят.",
                level=messages.WARNING,
            )
        if skipped := counts["skipped"] + counts["ALREADY_CANCELED"]:
            self.message_user(
                request,
                f"Пропущено: {skipped} — уже отменены.",
                level=messages.WARNING,
            )
