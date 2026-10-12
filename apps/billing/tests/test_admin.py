from __future__ import annotations

import datetime

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpRequest
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.billing.admin import AttendanceAdmin
from apps.billing.models import (
    Attendance,
    AttendanceCommentTag,
    AttendanceStatus,
    EnrollmentStatus,
    SubscriptionStatus,
)
from apps.billing.tests.factories import (
    AttendanceFactory,
    EnrollmentFactory,
    SubscriptionSlotFactory,
)
from apps.schedule.tests.factories import SubscriptionFactory

pytestmark = pytest.mark.django_db


def _request_with_messages(
    rf: RequestFactory, user: object, referer: str | None = None
) -> HttpRequest:
    headers = {"HTTP_REFERER": referer} if referer else {}
    request = rf.get("/", **headers)
    request.user = user  # type: ignore[assignment]
    SessionMiddleware(lambda r: None).process_request(request)  # type: ignore[arg-type]
    request.session.save()
    request._messages = FallbackStorage(request)  # noqa: SLF001
    return request


def _debitable_attendance() -> Attendance:
    attendance = AttendanceFactory(
        status=AttendanceStatus.ABSENT_OK,
        enrollment__status=EnrollmentStatus.ENROLLED,
        enrollment__subscription=SubscriptionFactory(
            status=SubscriptionStatus.ACTIVE,
            expires_at=timezone.now() + datetime.timedelta(days=30),
        ),
    )
    SubscriptionSlotFactory(
        subscription=attendance.enrollment.subscription,
        slot_id=attendance.enrollment.schedule_id,
        remaining_tokens=4,
    )
    return attendance


class TestFreezeSubscriptionAction:
    def test_renders_intermediate_form(self, admin_client) -> None:
        subscription = SubscriptionFactory(
            status=SubscriptionStatus.ACTIVE,
            expires_at=timezone.now() + datetime.timedelta(days=30),
        )
        url = reverse("admin:billing_subscription_changelist")

        response = admin_client.post(
            url,
            {"action": "freeze_subscriptions", "_selected_action": [subscription.pk]},
        )

        assert response.status_code == 200
        assert b"start_date" in response.content

    def test_apply_freezes_selected(self, admin_client) -> None:
        old_expiry = timezone.now() + datetime.timedelta(days=10)
        subscription = SubscriptionFactory(
            status=SubscriptionStatus.ACTIVE, expires_at=old_expiry
        )
        url = reverse("admin:billing_subscription_changelist")

        response = admin_client.post(
            url,
            {
                "action": "freeze_subscriptions",
                "_selected_action": [subscription.pk],
                "apply": "1",
                "start_date": "2026-07-10",
                "end_date": "2026-07-17",
                "reason": "Отпуск семьи",
            },
        )

        subscription.refresh_from_db()
        assert response.status_code == 302
        assert subscription.expires_at == old_expiry + datetime.timedelta(days=7)

    def test_service_validation_error_returns_to_form(self, admin_client) -> None:
        # ПОЧЕМУ: инвертированные даты отклоняет сервис, не форма —
        # админка бизнес-правил не знает
        old_expiry = timezone.now() + datetime.timedelta(days=10)
        subscription = SubscriptionFactory(
            status=SubscriptionStatus.ACTIVE, expires_at=old_expiry
        )
        url = reverse("admin:billing_subscription_changelist")

        response = admin_client.post(
            url,
            {
                "action": "freeze_subscriptions",
                "_selected_action": [subscription.pk],
                "apply": "1",
                "start_date": "2026-07-17",
                "end_date": "2026-07-10",
                "reason": "Инвертированные даты",
            },
        )

        subscription.refresh_from_db()
        assert response.status_code == 200
        assert subscription.expires_at == old_expiry


class TestAttendanceRowActions:
    def test_mark_attended_debits_token(self, admin_user, rf: RequestFactory) -> None:
        attendance = _debitable_attendance()
        model_admin = AttendanceAdmin(Attendance, AdminSite())
        request = _request_with_messages(rf, admin_user)

        response = model_admin.row_mark_attended(request, attendance.pk)

        attendance.refresh_from_db()
        assert response.status_code == 302
        assert attendance.status == AttendanceStatus.ATTENDED
        assert attendance.token_debited is True

    def test_billing_error_shows_message_not_500(
        self, admin_user, rf: RequestFactory
    ) -> None:
        # ПОЧЕМУ: отметка без баланса слота — сервис кидает BillingError,
        # админка обязана показать сообщение и не упасть в 500
        attendance = AttendanceFactory(
            status=AttendanceStatus.ABSENT_OK,
            enrollment__status=EnrollmentStatus.ENROLLED,
            enrollment__subscription=SubscriptionFactory(
                status=SubscriptionStatus.ACTIVE,
                expires_at=timezone.now() + datetime.timedelta(days=30),
            ),
        )
        model_admin = AttendanceAdmin(Attendance, AdminSite())
        request = _request_with_messages(rf, admin_user)

        response = model_admin.row_mark_attended(request, attendance.pk)

        attendance.refresh_from_db()
        assert response.status_code == 302
        assert attendance.token_debited is False

    def test_redirects_back_to_filtered_changelist(
        self, admin_user, rf: RequestFactory
    ) -> None:
        attendance = _debitable_attendance()
        model_admin = AttendanceAdmin(Attendance, AdminSite())
        referer = "http://testserver/admin/billing/attendance/?status=ATTENDED&p=3"
        request = _request_with_messages(rf, admin_user, referer=referer)

        response = model_admin.row_mark_attended(request, attendance.pk)

        assert response.status_code == 302
        assert response.url == referer

    def test_rejects_foreign_referer(self, admin_user, rf: RequestFactory) -> None:
        attendance = _debitable_attendance()
        model_admin = AttendanceAdmin(Attendance, AdminSite())
        request = _request_with_messages(
            rf, admin_user, referer="https://evil.example/phishing"
        )

        response = model_admin.row_mark_attended(request, attendance.pk)

        assert response.status_code == 302
        assert response.url == reverse("admin:billing_attendance_changelist")


class TestAttendanceChangeForm:
    def test_change_form_post_does_not_change_enrollment_or_date(
        self, admin_client
    ) -> None:
        # ПОЧЕМУ: смена записи в карточке переносила возврат фишки на чужой
        # абонемент — списали у ребёнка А, вернули ребёнку Б
        attendance = _debitable_attendance()
        original_enrollment_id = attendance.enrollment_id
        original_date = attendance.date
        foreign_enrollment = EnrollmentFactory()
        url = reverse("admin:billing_attendance_change", args=[attendance.pk])

        response = admin_client.post(
            url,
            {
                "enrollment": foreign_enrollment.pk,
                "date": (original_date - datetime.timedelta(days=7)).isoformat(),
                "comment": "Работал хорошо",
                "comment_tag": AttendanceCommentTag.POSITIVE,
                "_save": "1",
            },
        )

        attendance.refresh_from_db()
        assert response.status_code == 302
        assert attendance.enrollment_id == original_enrollment_id
        assert attendance.date == original_date
        assert attendance.comment == "Работал хорошо"


class TestAttendanceAddDeleteClosed:
    def test_add_forbidden_even_for_superuser(self, admin_client) -> None:
        response = admin_client.get(reverse("admin:billing_attendance_add"))

        assert response.status_code == 403

    def test_delete_forbidden_even_for_superuser(self, admin_client) -> None:
        # ПОЧЕМУ: удаление мимо set_attendance_status не возвращает фишку
        attendance = _debitable_attendance()
        url = reverse("admin:billing_attendance_delete", args=[attendance.pk])

        response = admin_client.post(url, {"post": "yes"})

        assert response.status_code == 403
        assert Attendance.objects.filter(pk=attendance.pk).exists()
