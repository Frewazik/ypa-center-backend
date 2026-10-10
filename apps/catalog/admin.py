from __future__ import annotations

from typing import TYPE_CHECKING

from django import forms
from django.contrib import admin
from django.http import HttpRequest

from unfold.widgets import UnfoldBooleanWidget

from apps.catalog.models import Activity
from apps.core.admin import RublesInputModelAdmin

# ПОЧЕМУ развилка: ModelForm[Activity] нужен mypy, а в рантайме класс
# не подписываемый (параметризация есть только в стабах django-stubs)
if TYPE_CHECKING:
    _ActivityModelForm = forms.ModelForm[Activity]
else:
    _ActivityModelForm = forms.ModelForm


def _sets_free_price(form: forms.ModelForm[Activity]) -> bool:
    # ПОЧЕМУ только новая цена 0: подтверждение нужно в момент решения,
    # а не при каждой правке описания уже бесплатного кружка
    price = form.cleaned_data.get("price")
    return price == 0 and (form.instance.pk is None or "price" in form.changed_data)


class ActivityAdminForm(_ActivityModelForm):
    # ПОЧЕМУ: цена кружка — это и цена пробного; при 0 пробное подтверждается
    # сразу, без оплаты (create_trial_payment). default=0 у поля делает такую
    # цену лёгкой ошибкой «забыл заполнить»
    confirm_free_trial = forms.BooleanField(
        label="Да, пробное бесплатное",
        required=False,
        widget=UnfoldBooleanWidget,
        help_text="Отметьте, если цена 0 ₽ поставлена намеренно.",
    )

    class Meta:
        model = Activity
        fields = "__all__"

    def clean(self) -> dict[str, object]:
        super().clean()
        if _sets_free_price(self) and not self.cleaned_data.get("confirm_free_trial"):
            self.add_error(
                "price",
                "Цена 0 ₽ — пробное по этому кружку станет бесплатным: запись "
                "подтверждается сразу, без оплаты. Если так и задумано, отметьте "
                "«Да, пробное бесплатное».",
            )
        return self.cleaned_data


class ActivityChangelistForm(_ActivityModelForm):
    # ПОЧЕМУ: в строке списка галочки подтверждения нет — цену 0 ставят
    # только в карточке кружка, где её видно
    def clean(self) -> dict[str, object]:
        super().clean()
        if _sets_free_price(self):
            self.add_error(
                "price",
                "Цену 0 ₽ (бесплатное пробное) ставьте в карточке кружка.",
            )
        return self.cleaned_data


@admin.register(Activity)
class ActivityAdmin(RublesInputModelAdmin):
    form = ActivityAdminForm
    list_display = ("name", "slug", "category", "price", "is_active")
    list_editable = ("price", "is_active")
    list_filter = ("is_active", "category")
    search_fields = ("name", "slug")
    search_help_text = "Название или адрес в ссылке"
    prepopulated_fields = {"slug": ("name",)}
    rubles_fields = {"price": "Цена, ₽"}

    def get_changelist_form(
        self, request: HttpRequest, **kwargs: object
    ) -> type[forms.ModelForm[Activity]]:
        return super().get_changelist_form(
            request, form=ActivityChangelistForm, **kwargs
        )
