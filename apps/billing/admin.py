from __future__ import annotations

import uuid

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from unfold.admin import ModelAdmin
from unfold.contrib.filters.admin import (
    ChoicesDropdownFilter,
    RangeDateFilter,
    RelatedDropdownFilter,
)
from unfold.decorators import action, display
from unfold.widgets import UnfoldAdminSingleDateWidget, UnfoldAdminTextareaWidget

from apps.billing import services
from apps.billing.models import (
    Attendance,
    AttendanceCommentTag,
    AttendanceStatus,
    Enrollment,
    EnrollmentStatus,
    EnrollmentType,
    RefundStatus,
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
    Transaction,
    TransactionStatus,
)
from apps.billing.services import REFUND_STATUSES_AWAITING_MANUAL, BillingError


class FreezeSubscriptionActionForm(forms.Form):
    # ПОЧЕМУ: без clean() — бизнес-валидация периода живёт в сервисе,
    # форма только собирает данные
    start_date = forms.DateField(
        label="Начало заморозки", widget=UnfoldAdminSingleDateWidget
    )
    end_date = forms.DateField(
        label="Конец заморозки", widget=UnfoldAdminSingleDateWidget
    )
    reason = forms.CharField(label="Причина", widget=UnfoldAdminTextareaWidget)


@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(ModelAdmin):
    # ПОЧЕМУ: менять цену тарифа безопасно — активные абонементы считаются
    # по зафиксированному Subscription.purchase_price, витрина инвалидируется
    # сигналом public_api мгновенно
    list_display = (
        "id",
        "name",
        "slots_count",
        "price",
        "base_session_price",
        "is_unlimited",
        "is_active",
    )
    list_editable = ("price", "base_session_price", "is_active")
    list_filter = ("is_active", "is_unlimited")
    search_fields = ("name",)
    ordering = ("id",)


@admin.register(Subscription)
class SubscriptionAdmin(ModelAdmin):
    list_display = (
        "id",
        "parent",
        "plan",
        "display_status",
        "purchase_price",
        "created_at",
        "expires_at",
    )
    list_filter = (
        ("status", ChoicesDropdownFilter),
        ("created_at", RangeDateFilter),
    )
    list_select_related = ("parent", "plan")
    autocomplete_fields = ("parent", "plan")
    search_fields = ("parent__email", "parent__full_name", "parent__phone")
    # ПОЧЕМУ: цены зафиксированы на момент покупки, правка руками
    # разъедется с суммой транзакции
    readonly_fields = ("purchase_price", "base_session_price", "created_at")
    actions = ("freeze_subscriptions",)

    @display(
        description="Статус",
        label={
            SubscriptionStatus.ACTIVE: "success",
            SubscriptionStatus.PENDING: "warning",
            SubscriptionStatus.DRAFT: "info",
            SubscriptionStatus.EXPIRED: "danger",
            SubscriptionStatus.CANCELED: "danger",
        },
    )
    def display_status(self, obj: Subscription) -> str:
        return obj.status

    @admin.action(description="Заморозить абонемент")
    def freeze_subscriptions(
        self,
        request: HttpRequest,
        queryset: QuerySet[Subscription],
    ) -> TemplateResponse | None:
        form = FreezeSubscriptionActionForm(
            request.POST if "apply" in request.POST else None
        )

        if "apply" in request.POST and form.is_valid():
            try:
                result = services.bulk_freeze_subscriptions(
                    subscription_ids=list(queryset.values_list("pk", flat=True)),
                    start_date=form.cleaned_data["start_date"],
                    end_date=form.cleaned_data["end_date"],
                    reason=form.cleaned_data["reason"],
                )
            except BillingError as exc:
                form.add_error(None, str(exc))
            else:
                for error in result.errors:
                    self.message_user(request, error, level=messages.WARNING)
                if result.frozen_count:
                    self.message_user(
                        request,
                        f"Заморожено абонементов: {result.frozen_count} "
                        f"(сдвиг на {result.frozen_days} дн.).",
                        level=messages.SUCCESS,
                    )
                return None

        return TemplateResponse(
            request,
            "admin/billing/freeze_subscription.html",
            {
                **self.admin_site.each_context(request),
                "title": "Заморозка абонементов",
                "form": form,
                "subscriptions": queryset,
                "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
                "opts": self.model._meta,
            },
        )


class RefundStateFilter(admin.SimpleListFilter):
    title = "Возврат"
    parameter_name = "refund"

    def lookups(
        self, request: HttpRequest, model_admin: admin.ModelAdmin[Transaction]
    ) -> list[tuple[str, str]]:
        return [
            ("queued", "В очереди"),
            ("in_progress", "В обработке у ЮКассы"),
            ("manual", "Нужен ручной разбор"),
            ("done", "Выполнен"),
        ]

    def queryset(
        self, request: HttpRequest, queryset: QuerySet[Transaction]
    ) -> QuerySet[Transaction]:
        if self.value() == "queued":
            return queryset.filter(requires_compensation=True)
        if self.value() == "in_progress":
            return queryset.filter(refund_status=RefundStatus.PENDING)
        if self.value() == "manual":
            return queryset.filter(
                requires_compensation=False,
                refund_status__in=REFUND_STATUSES_AWAITING_MANUAL,
            )
        if self.value() == "done":
            return queryset.filter(
                refund_status__in=(RefundStatus.SUCCEEDED, RefundStatus.MANUAL)
            )
        return queryset


@admin.register(Transaction)
class TransactionAdmin(ModelAdmin):
    # ПОЧЕМУ read-only: деньги двигают только сервисы (вебхук, возвраты);
    # единственное ручное действие — закрыть разобранный возврат кнопкой строки
    list_display = (
        "created_at",
        "parent",
        "amount",
        "received_amount",
        "display_status",
        "display_refund",
        "refund_error",
        "external_id",
    )
    list_filter = (
        RefundStateFilter,
        ("status", ChoicesDropdownFilter),
        ("created_at", RangeDateFilter),
    )
    list_select_related = ("parent",)
    search_fields = ("parent__email", "parent__phone", "external_id")
    ordering = ("-created_at",)
    actions_row = ("row_resolve_refund",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: Transaction | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: Transaction | None = None
    ) -> bool:
        return False

    @display(
        description="Статус",
        label={
            TransactionStatus.SUCCEEDED: "success",
            TransactionStatus.PENDING: "warning",
            TransactionStatus.FAILED: "danger",
            TransactionStatus.CANCELED: "danger",
        },
    )
    def display_status(self, obj: Transaction) -> str:
        return obj.status

    @display(description="Возврат")
    def display_refund(self, obj: Transaction) -> str:
        if obj.requires_compensation:
            return "В очереди"
        if obj.refund_status in REFUND_STATUSES_AWAITING_MANUAL:
            return f"Нужен ручной разбор ({obj.get_refund_status_display()})"
        return obj.get_refund_status_display() or "—"

    @admin.display(description="Причина")
    def refund_error(self, obj: Transaction) -> str:
        return str(obj.metadata.get("refund_error", ""))

    # ПОЧЕМУ отдельный метод, а не "change": has_change_permission закрыт ради
    # read-only формы, а закрытие возврата — отдельное осознанное действие.
    # Не "billing.change_transaction" строкой: системная проверка unfold ищет
    # такое право в auth_permission и роняет migrate на пустой базе
    def has_resolve_refund_permission(
        self, request: HttpRequest, object_id: str | None = None
    ) -> bool:
        return request.user.has_perm("billing.change_transaction")

    @action(
        description="Возврат разобран вручную",
        permissions=["resolve_refund"],
    )
    def row_resolve_refund(
        self, request: HttpRequest, object_id: str
    ) -> HttpResponseRedirect:
        try:
            services.resolve_refund_manually(
                uuid.UUID(object_id), resolved_by=request.user.get_username()
            )
        except (BillingError, ValueError) as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(
                request,
                f"Возврат по транзакции {object_id} закрыт вручную.",
                level=messages.SUCCESS,
            )
        return redirect(reverse("admin:billing_transaction_changelist"))


@admin.register(Enrollment)
class EnrollmentAdmin(ModelAdmin):
    # ПОЧЕМУ read-only: записи создаются только чекаутом, статусы двигают
    # сервисы (оплата/свипер) — ручная правка разъехалась бы со счётчиком мест
    list_display = (
        "id",
        "display_type",
        "student",
        "schedule",
        "trial_date",
        "display_status",
        "created_at",
    )
    list_filter = (
        ("type", ChoicesDropdownFilter),
        ("status", ChoicesDropdownFilter),
        ("trial_date", RangeDateFilter),
    )
    list_select_related = ("student", "schedule__activity", "activity")
    search_fields = (
        "student__full_name",
        "student__parent__email",
        "student__parent__phone",
    )
    ordering = ("-created_at",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: Enrollment | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: Enrollment | None = None
    ) -> bool:
        return False

    @display(
        description="Тип",
        label={
            EnrollmentType.TRIAL: "warning",
            EnrollmentType.REGULAR: "info",
        },
    )
    def display_type(self, obj: Enrollment) -> str:
        return obj.type

    @display(
        description="Статус",
        label={
            EnrollmentStatus.ENROLLED: "success",
            EnrollmentStatus.HELD: "warning",
            EnrollmentStatus.CANCELED: "danger",
        },
    )
    def display_status(self, obj: Enrollment) -> str:
        return obj.status


@admin.register(Attendance)
class AttendanceAdmin(ModelAdmin):
    list_display = (
        "id",
        "student_name",
        "group_name",
        "date",
        "display_status",
        "token_debited",
        "display_comment_tag",
    )
    list_filter = (
        ("date", RangeDateFilter),
        ("enrollment__schedule", RelatedDropdownFilter),
        ("status", ChoicesDropdownFilter),
    )
    # ПОЧЕМУ: student_name/group_name ходят по FK на каждую строку,
    # без JOIN список превращается в N+1
    list_select_related = (
        "enrollment",
        "enrollment__student",
        "enrollment__schedule",
        "enrollment__schedule__activity",
    )
    search_fields = ("enrollment__student__full_name",)
    # ПОЧЕМУ status read-only: сохранение формы писало бы поле напрямую, мимо
    # set_attendance_status — без списания и возврата фишки. Статус меняется
    # только кнопками строки
    readonly_fields = ("status", "token_debited", "created_at")
    actions_row = ("row_mark_attended", "row_mark_absent_ok", "row_mark_absent_err")

    def get_queryset(self, request: HttpRequest) -> QuerySet[Attendance]:
        # ПОЧЕМУ: роль «Учитель» видит посещаемость только своих групп;
        # суперюзер (teacher_profile отсутствует) видит всё
        queryset = super().get_queryset(request)
        if request.user.is_superuser:
            return queryset
        teacher = getattr(request.user, "teacher_profile", None)
        if teacher is None:
            return queryset
        return queryset.filter(enrollment__schedule__teacher=teacher)

    @admin.display(description="Ребёнок", ordering="enrollment__student__full_name")
    def student_name(self, obj: Attendance) -> str:
        return obj.enrollment.student.full_name

    @admin.display(description="Группа")
    def group_name(self, obj: Attendance) -> str:
        schedule = obj.enrollment.schedule
        return schedule.group_name or schedule.activity.name

    @display(
        description="Статус",
        label={
            AttendanceStatus.ATTENDED: "success",
            AttendanceStatus.ABSENT_OK: "warning",
            AttendanceStatus.ABSENT_ERR: "danger",
        },
    )
    def display_status(self, obj: Attendance) -> str:
        return obj.status

    @display(
        description="Тон комментария",
        label={
            AttendanceCommentTag.POSITIVE: "success",
            AttendanceCommentTag.NEGATIVE: "danger",
            AttendanceCommentTag.NEUTRAL: "info",
        },
    )
    def display_comment_tag(self, obj: Attendance) -> str:
        return obj.comment_tag

    @action(description="Присутствовал", permissions=["change"])
    def row_mark_attended(
        self, request: HttpRequest, object_id: int
    ) -> HttpResponseRedirect:
        return self._set_status(request, object_id, AttendanceStatus.ATTENDED)

    @action(description="Отсутствовал (ув.)", permissions=["change"])
    def row_mark_absent_ok(
        self, request: HttpRequest, object_id: int
    ) -> HttpResponseRedirect:
        return self._set_status(request, object_id, AttendanceStatus.ABSENT_OK)

    @action(description="Отметка ошибочна", permissions=["change"])
    def row_mark_absent_err(
        self, request: HttpRequest, object_id: int
    ) -> HttpResponseRedirect:
        return self._set_status(request, object_id, AttendanceStatus.ABSENT_ERR)

    def _set_status(
        self,
        request: HttpRequest,
        object_id: int,
        status: AttendanceStatus,
    ) -> HttpResponseRedirect:
        try:
            attendance = services.set_attendance_status(
                attendance_id=int(object_id), status=status
            )
        except BillingError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            token_note = (
                " Фишка списана." if attendance.token_debited else " Фишка не списана."
            )
            self.message_user(
                request,
                f"Отметка #{attendance.pk}: статус "
                f"«{attendance.get_status_display()}».{token_note}",
                level=messages.SUCCESS,
            )
        # ПОЧЕМУ: referer возвращает менеджера в исходный контекст (фильтры,
        # пагинация), но контролируется клиентом — без проверки хоста
        # это open redirect
        referer = request.META.get("HTTP_REFERER", "")
        if referer and url_has_allowed_host_and_scheme(
            referer,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return redirect(referer)
        return redirect(reverse("admin:billing_attendance_changelist"))
