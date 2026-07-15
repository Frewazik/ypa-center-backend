from __future__ import annotations

import json

import pytest
from django.core.cache import cache
from django.test import RequestFactory

from apps.billing.models import EnrollmentStatus, TransactionStatus
from apps.core.dashboard import DASHBOARD_CACHE_KEY, dashboard_callback
from apps.schedule.tests.factories import EnrollmentFactory, ParentFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_dashboard_cache():
    # ПОЧЕМУ: метрики кэшируются — без очистки данные протекают между тестами
    cache.delete(DASHBOARD_CACHE_KEY)
    yield
    cache.delete(DASHBOARD_CACHE_KEY)


def _succeeded_transaction(amount: int):
    from apps.billing.models import Transaction

    return Transaction.objects.create(
        parent=ParentFactory(),
        amount=amount,
        status=TransactionStatus.SUCCEEDED,
    )


class TestDashboardCallback:
    def test_returns_kpi_and_charts(self, rf: RequestFactory, admin_user) -> None:
        _succeeded_transaction(700_000)
        EnrollmentFactory(status=EnrollmentStatus.ENROLLED)
        request = rf.get("/admin/")
        request.user = admin_user

        context = dashboard_callback(request, {})

        assert context["kpi"][0]["title"] == "Выручка за месяц"
        assert "7 000" in context["kpi"][0]["metric"]
        assert int(context["kpi"][1]["metric"]) == 1

        payments = json.loads(context["payments_chart"])
        assert len(payments["labels"]) == 30

        top_groups = json.loads(context["top_groups_chart"])
        assert len(top_groups["labels"]) == 1

    def test_held_enrollments_not_counted(self, rf: RequestFactory, admin_user) -> None:
        # ПОЧЕМУ: HELD — 15-минутная бронь до оплаты, не «активный ученик»
        EnrollmentFactory(status=EnrollmentStatus.HELD)
        request = rf.get("/admin/")
        request.user = admin_user

        context = dashboard_callback(request, {})

        assert int(context["kpi"][1]["metric"]) == 0

    def test_serves_metrics_from_cache_on_second_call(
        self, rf: RequestFactory, admin_user, django_assert_num_queries
    ) -> None:
        _succeeded_transaction(700_000)
        request = rf.get("/admin/")
        request.user = admin_user

        dashboard_callback(request, {})

        # ПОЧЕМУ: повторный рендер не должен трогать БД, метрики берутся из кэша
        with django_assert_num_queries(0):
            context = dashboard_callback(request, {})

        assert "7 000" in context["kpi"][0]["metric"]
