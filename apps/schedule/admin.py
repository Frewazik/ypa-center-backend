# ПОЧЕМУ формы с проверками: пересечения групп охраняют exclusion-ограничения
# БД, но Django их в форме не видит (день и время группы — копии из слота),
# и без проверки до сохранения админка отвечала 500. Маски создаются и
# удаляются только сервисами: валидация коллизий, локи и письма семьям

from __future__ import annotations

from typing import TYPE_CHECKING

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html_join

from unfold.admin import ModelAdmin
from unfold.decorators import action
from unfold.widgets import UnfoldBooleanWidget

from apps.schedule.models import MaskType, Room, Schedule, ScheduleMask, TimeSlot
from apps.schedule.services import (
    FamilyContact,
    GridPlacement,
    create_schedule_mask,
    delete_schedule_mask,
    families_to_warn,
    validate_grid_change,
)

# ПОЧЕМУ развилка: ModelForm[X] нужен mypy, а в рантайме класс
# не подписываемый (параметризация есть только в стабах django-stubs)
if TYPE_CHECKING:
    _ScheduleModelForm = forms.ModelForm[Schedule]
    _TimeSlotModelForm = forms.ModelForm[TimeSlot]
    _MaskModelForm = forms.ModelForm[ScheduleMask]
else:
    _ScheduleModelForm = _TimeSlotModelForm = _MaskModelForm = forms.ModelForm


def _add_service_errors(form: forms.BaseForm, exc: ValidationError) -> None:
    # ПОЧЕМУ: сервис называет поля модели; поля, которого нет в форме
    # (кабинет при выключенных кабинетах), ошибка уходит наверх формы
    if not hasattr(exc, "error_dict"):
        form.add_error(None, exc)
        return
    for field, errors in exc.error_dict.items():
        form.add_error(field if field in form.fields else None, errors)


def _confirm_field() -> forms.BooleanField:
    return forms.BooleanField(
        label="Да, переносим",
        required=False,
        widget=UnfoldBooleanWidget,
        help_text="Отметьте, если группа переезжает на другой день насовсем.",
    )


def _contact_line(contact: FamilyContact, *, refund_trial: bool) -> str:
    if contact.trial_date is None:
        kind = "абонемент"
    else:
        kind = f"пробное {contact.trial_date:%d.%m}"
        if refund_trial:
            kind += " — вернуть оплату вручную"
    phone = contact.phone or "телефона нет"
    return (
        f"«{contact.group}»: {contact.child_name} — {contact.parent_name}, "
        f"{phone}, {contact.email} ({kind})"
    )


def _day_change_warning(schedule_ids: list[int]) -> list[str]:
    # ПОЧЕМУ: решение бизнеса — постоянный переезд группы на другой день
    # система не запрещает, но переспрашивает и даёт список, кого обзвонить
    contacts = families_to_warn(schedule_ids, on_date=None)
    lines = [_contact_line(contact, refund_trial=False) for contact in contacts]
    return [
        "Группа переедет на другой день недели насовсем. Кого предупредить:",
        *(lines or ["записанных семей нет."]),
        "Если всё верно, отметьте «Да, переносим» и сохраните ещё раз.",
    ]


@admin.register(Room)
class RoomAdmin(ModelAdmin):
    list_display = ("name", "is_active")
    list_editable = ("is_active",)
    search_fields = ("name",)
    search_help_text = "Название кабинета"

    def get_model_perms(self, request: HttpRequest) -> dict[str, bool]:
        # ПОЧЕМУ: пустые права прячут раздел из меню, но модель остаётся
        # зарегистрированной — автокомплит кабинета в формах работает
        if not settings.SCHEDULE_ROOMS_ENABLED:
            return {}
        return super().get_model_perms(request)


class TimeSlotAdminForm(_TimeSlotModelForm):
    confirm_day_change = _confirm_field()

    class Meta:
        model = TimeSlot
        fields = "__all__"

    def clean(self) -> dict[str, object]:
        super().clean()
        slot = self.instance
        if self.errors or slot.pk is None:
            return self.cleaned_data
        day = self.cleaned_data["day_of_week"]
        start = self.cleaned_data["start_time"]
        end = self.cleaned_data["end_time"]
        # ПОЧЕМУ instance: до _post_clean в нём ещё значения из БД
        if (day, start, end) == (slot.day_of_week, slot.start_time, slot.end_time):
            return self.cleaned_data
        groups = list(Schedule.objects.filter(time_slot=slot).order_by("pk"))
        if not groups:
            return self.cleaned_data
        day_changed = day != slot.day_of_week
        placements = [
            GridPlacement(
                label=str(group),
                day_of_week=day,
                start_time=start,
                end_time=end,
                teacher_id=group.teacher_id,
                room_id=group.room_id,
            )
            for group in groups
            if group.is_active
        ]
        group_ids = [group.pk for group in groups]
        try:
            validate_grid_change(
                placements, moving_ids=group_ids, day_changed=day_changed
            )
        except ValidationError as exc:
            # ПОЧЕМУ наверх: в форме слота нет полей преподавателя и кабинета
            self.add_error(None, exc.messages)
            return self.cleaned_data
        if day_changed and not self.cleaned_data.get("confirm_day_change"):
            self.add_error("day_of_week", _day_change_warning(group_ids))
        return self.cleaned_data


@admin.register(TimeSlot)
class TimeSlotAdmin(ModelAdmin):
    form = TimeSlotAdminForm
    list_display = ("id", "day_of_week", "start_time", "end_time")
    list_filter = ("day_of_week",)
    ordering = ("day_of_week", "start_time")

    def get_fields(
        self, request: HttpRequest, obj: TimeSlot | None = None
    ) -> list[str]:
        fields = ["day_of_week", "start_time", "end_time"]
        if obj is not None:
            fields.append("confirm_day_change")
        return fields


class ScheduleAdminForm(_ScheduleModelForm):
    confirm_day_change = _confirm_field()

    class Meta:
        model = Schedule
        fields = "__all__"

    def clean(self) -> dict[str, object]:
        super().clean()
        if self.errors:
            return self.cleaned_data
        group = self.instance
        grid_fields = ("time_slot", "teacher", "room", "is_active")
        if group.pk is not None and not set(grid_fields) & set(self.changed_data):
            return self.cleaned_data
        # ПОЧЕМУ: в строке списка редактируется только часть полей, а кабинет
        # скрыт при выключенных кабинетах — остальное из сохранённой группы
        data = self.cleaned_data
        slot = data["time_slot"] if "time_slot" in self.fields else group.time_slot
        teacher = data["teacher"] if "teacher" in self.fields else group.teacher
        room = data["room"] if "room" in self.fields else group.room
        is_active = data["is_active"] if "is_active" in self.fields else group.is_active
        day_changed = group.pk is not None and slot.day_of_week != group.day_of_week
        placements = (
            [
                GridPlacement(
                    label="",
                    day_of_week=slot.day_of_week,
                    start_time=slot.start_time,
                    end_time=slot.end_time,
                    teacher_id=teacher.pk if teacher is not None else None,
                    room_id=room.pk if room is not None else None,
                )
            ]
            if is_active
            else []
        )
        moving_ids = [group.pk] if group.pk is not None else []
        try:
            validate_grid_change(
                placements, moving_ids=moving_ids, day_changed=day_changed
            )
        except ValidationError as exc:
            _add_service_errors(self, exc)
            return self.cleaned_data
        if (
            day_changed
            and "confirm_day_change" in self.fields
            and not self.cleaned_data.get("confirm_day_change")
        ):
            self.add_error("time_slot", _day_change_warning(moving_ids))
        return self.cleaned_data


class ScheduleChangelistForm(ScheduleAdminForm):
    # ПОЧЕМУ: слот в строке списка не меняется — переспрашивать нечего,
    # а пересечение при включении группы проверяется так же, как в карточке.
    # ПОЧЕМУ ignore: None — штатный способ Django убрать унаследованное поле
    # формы, а стабы ждут здесь BooleanField
    confirm_day_change = None  # type: ignore[assignment]


@admin.register(Schedule)
class ScheduleAdmin(ModelAdmin):
    form = ScheduleAdminForm
    list_display = (
        "group_name",
        "activity",
        "teacher",
        "room",
        "day_of_week",
        "start_time",
        "end_time",
        "age_min",
        "age_max",
        "max_capacity",
        "is_active",
    )
    list_editable = ("age_min", "age_max", "max_capacity", "is_active")
    list_filter = ("is_active", "day_of_week")
    search_fields = ("group_name", "activity__name")
    search_help_text = "Группа или кружок"
    list_select_related = ("activity", "teacher__user", "room", "time_slot")
    autocomplete_fields = ("activity", "teacher", "room")
    actions_detail = ["open_mask_form"]

    def get_list_display(self, request: HttpRequest) -> tuple[str, ...]:
        return _without_rooms(tuple(super().get_list_display(request)), "room")

    def get_fields(
        self, request: HttpRequest, obj: Schedule | None = None
    ) -> list[str]:
        fields = [
            name
            for name in super().get_fields(request, obj)
            if name != "confirm_day_change"
        ]
        if obj is not None:
            # ПОЧЕМУ: галочка нужна только при правке — новой группе
            # переезжать неоткуда
            fields.append("confirm_day_change")
        return list(_without_rooms(tuple(fields), "room"))

    def get_changelist_form(
        self, request: HttpRequest, **kwargs: object
    ) -> type[forms.ModelForm[Schedule]]:
        return super().get_changelist_form(
            request, form=ScheduleChangelistForm, **kwargs
        )

    def changelist_view(
        self, request: HttpRequest, extra_context: dict[str, object] | None = None
    ) -> HttpResponse:
        # ПОЧЕМУ: Django проверяет формы строк списка вне транзакции, а локи
        # проверки пересечений обязаны дожить до сохранения
        with transaction.atomic():
            return super().changelist_view(request, extra_context)

    @action(description="Перенести или отменить занятие")
    def open_mask_form(
        self, request: HttpRequest, object_id: str
    ) -> HttpResponseRedirect:
        url = reverse("admin:schedule_schedulemask_add")
        return redirect(f"{url}?schedule={int(object_id)}")


class UpcomingMaskFilter(admin.SimpleListFilter):
    title = "Дата"
    parameter_name = "when"

    def lookups(
        self, request: HttpRequest, model_admin: admin.ModelAdmin[ScheduleMask]
    ) -> list[tuple[str, str]]:
        return [("upcoming", "Сегодня и позже"), ("past", "Прошедшие")]

    def queryset(
        self, request: HttpRequest, queryset: QuerySet[ScheduleMask]
    ) -> QuerySet[ScheduleMask]:
        today = timezone.localdate()
        if self.value() == "upcoming":
            return queryset.filter(target_date__gte=today)
        if self.value() == "past":
            return queryset.filter(target_date__lt=today)
        return queryset


class ScheduleMaskAdminForm(_MaskModelForm):
    class Meta:
        model = ScheduleMask
        fields = (
            "schedule",
            "target_date",
            "type",
            "new_day_of_week",
            "new_start_time",
            "new_end_time",
            "new_room",
            "new_teacher",
        )

    def clean(self) -> dict[str, object]:
        # ПОЧЕМУ здесь, а не в save_model: ошибки сервиса (кабинет занят,
        # дата в прошлом) должны вернуться в форму, а save_model их уже не
        # покажет. Админка держит запрос в одной транзакции — если дальше
        # что-то упадёт, маска откатится вместе с ним
        super().clean()
        if self.errors:
            return self.cleaned_data
        data = self.cleaned_data
        try:
            self.instance = create_schedule_mask(
                schedule=data["schedule"],
                target_date=data["target_date"],
                mask_type=MaskType(data["type"]),
                new_day_of_week=data.get("new_day_of_week"),
                new_start_time=data.get("new_start_time"),
                new_end_time=data.get("new_end_time"),
                new_room=data.get("new_room"),
                new_teacher=data.get("new_teacher"),
            )
        except ValidationError as exc:
            _add_service_errors(self, exc)
        return self.cleaned_data


@admin.register(ScheduleMask)
class ScheduleMaskAdmin(ModelAdmin):
    form = ScheduleMaskAdminForm
    list_display = (
        "target_date",
        "type",
        "schedule",
        "new_day_of_week",
        "new_start_time",
        "new_end_time",
        "new_room",
        "new_teacher",
    )
    list_filter = (UpcomingMaskFilter, "type")
    list_select_related = (
        "schedule__activity",
        "new_room",
        "new_teacher__user",
    )
    search_fields = ("schedule__group_name", "schedule__activity__name")
    search_help_text = "Группа или кружок"
    autocomplete_fields = ("schedule", "new_room", "new_teacher")
    ordering = ("-target_date", "-pk")
    readonly_fields = ("families",)

    def get_list_display(self, request: HttpRequest) -> tuple[str, ...]:
        return _without_rooms(tuple(super().get_list_display(request)), "new_room")

    def get_fields(
        self, request: HttpRequest, obj: ScheduleMask | None = None
    ) -> list[str]:
        fields = list(ScheduleMaskAdminForm.Meta.fields)
        if obj is not None:
            fields.append("families")
        return list(_without_rooms(tuple(fields), "new_room"))

    # ПОЧЕМУ без правки: изменение маски — это новая проверка коллизий;
    # проще и надёжнее удалить и создать заново через сервис
    def has_change_permission(
        self, request: HttpRequest, obj: ScheduleMask | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: ScheduleMask | None = None
    ) -> bool:
        if obj is not None and obj.target_date <= timezone.localdate():
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request: HttpRequest) -> dict[str, object]:
        # ПОЧЕМУ: массовое удаление идёт через QuerySet.delete() в обход
        # сервиса — без проверки даты и писем семьям
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def save_model(
        self,
        request: HttpRequest,
        obj: ScheduleMask,
        form: forms.ModelForm[ScheduleMask],
        change: bool,
    ) -> None:
        # ПОЧЕМУ пусто: маску уже сохранил create_schedule_mask в _post_clean
        # формы; повторный save() обошёл бы сервис
        return

    def delete_model(self, request: HttpRequest, obj: ScheduleMask) -> None:
        delete_schedule_mask(mask_id=obj.pk)

    def response_delete(
        self, request: HttpRequest, obj_display: str, obj_id: int
    ) -> HttpResponse:
        # ПОЧЕМУ: Django ведёт в список только при праве правки, которое здесь
        # закрыто, — без этого после удаления администратор попадал на главную
        response = super().response_delete(request, obj_display, obj_id)
        if isinstance(response, HttpResponseRedirect):
            return redirect(reverse("admin:schedule_schedulemask_changelist"))
        return response

    def response_add(
        self,
        request: HttpRequest,
        obj: ScheduleMask,
        post_url_continue: str | None = None,
    ) -> HttpResponse:
        # ПОЧЕМУ на карточку: там список семей, которых нужно обзвонить
        self.message_user(
            request,
            f"Сохранено: {obj}. Письма записанным семьям уйдут автоматически; "
            "ниже — кого обзвонить.",
            level=messages.SUCCESS,
        )
        return redirect(reverse("admin:schedule_schedulemask_change", args=[obj.pk]))

    @admin.display(description="Кого предупредить")
    def families(self, obj: ScheduleMask) -> str:
        contacts = families_to_warn([obj.schedule_id], on_date=obj.target_date)
        if not contacts:
            return "Записанных семей нет."
        refund_trial = obj.type == MaskType.CANCELLATION
        return format_html_join(
            "\n",
            "<div>{}</div>",
            (
                (_contact_line(contact, refund_trial=refund_trial),)
                for contact in contacts
            ),
        )


def _without_rooms(fields: tuple[str, ...], room_field: str) -> tuple[str, ...]:
    if settings.SCHEDULE_ROOMS_ENABLED:
        return fields
    return tuple(name for name in fields if name != room_field)
