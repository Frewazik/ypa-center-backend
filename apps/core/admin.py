from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import ClassVar

from django import forms
from django.contrib import admin
from django.db import models
from django.http import HttpRequest

from unfold.admin import ModelAdmin
from unfold.widgets import UnfoldAdminDecimalFieldWidget

# ПОЧЕМУ: суммы в БД — целые копейки в IntegerField (int4); верхняя граница
# ввода в рублях не даёт форме принять сумму, которую база отвергнет
_MAX_KOPECKS = 2**31 - 1
_KOPECKS_IN_RUBLE = 100


def format_rubles(amount_kopecks: int) -> str:
    rubles, kopecks = divmod(amount_kopecks, _KOPECKS_IN_RUBLE)
    whole = f"{rubles:,}".replace(",", " ")
    if kopecks:
        return f"{whole},{kopecks:02d} ₽"
    return f"{whole} ₽"


def rubles_column(field_name: str, description: str) -> Callable[[models.Model], str]:
    @admin.display(description=description, ordering=field_name)
    def _column(obj: models.Model) -> str:
        value = getattr(obj, field_name)
        if value is None:
            return "—"
        if not isinstance(value, int):
            raise TypeError(f"{field_name}: ожидались копейки int, а не {value!r}")
        return format_rubles(value)

    return _column


class RublesField(forms.DecimalField):
    """Ввод суммы в рублях для поля, хранящего целые копейки."""

    def __init__(self, *, label: str, required: bool) -> None:
        super().__init__(
            label=label,
            required=required,
            help_text="Сумма в рублях.",
            max_digits=12,
            decimal_places=2,
            min_value=Decimal(0),
            max_value=Decimal(_MAX_KOPECKS) / _KOPECKS_IN_RUBLE,
            widget=UnfoldAdminDecimalFieldWidget(attrs={"step": "0.01"}),
        )

    def prepare_value(self, value: object) -> object:
        # ПОЧЕМУ isinstance: из модели приходят копейки (int), а при повторном
        # показе формы с ошибкой — строка, которую человек ввёл в рублях
        if isinstance(value, int) and not isinstance(value, bool):
            return Decimal(value) / _KOPECKS_IN_RUBLE
        return super().prepare_value(value)

    def clean(self, value: object) -> int | None:
        rubles = super().clean(value)
        if rubles is None:
            return None
        return int(rubles * _KOPECKS_IN_RUBLE)

    def has_changed(self, initial: object, data: object) -> bool:
        # ПОЧЕМУ: базовый has_changed сравнил бы копейки из БД с рублями из
        # формы и считал поле изменённым всегда
        try:
            new_value = self.clean(data)
        except forms.ValidationError:
            return True
        return initial != new_value


class RublesInputModelAdmin(ModelAdmin):
    """Поля-копейки из `rubles_fields` вводятся в админке в рублях.

    Ключ — имя поля модели, значение — подпись поля в форме.
    """

    rubles_fields: ClassVar[Mapping[str, str]] = {}

    def formfield_for_dbfield(
        self,
        db_field: models.Field[object, object],
        request: HttpRequest,
        **kwargs: object,
    ) -> forms.Field | None:
        label = self.rubles_fields.get(db_field.name)
        if label is not None:
            return RublesField(label=label, required=not db_field.blank)
        return super().formfield_for_dbfield(db_field, request, **kwargs)
