from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, TypedDict

from django.core.cache import cache
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.http import HttpRequest
from django.utils import timezone

from apps.billing.models import (
    Enrollment,
    EnrollmentStatus,
    Transaction,
    TransactionStatus,
)
from apps.users.models import Student

DASHBOARD_CACHE_KEY = "admin:dashboard:metrics:v1"
DASHBOARD_CACHE_TTL = 600


class KpiCard(TypedDict):
    title: str
    metric: str


class ChartDataset(TypedDict):
    label: str
    data: list[float]


class ChartData(TypedDict):
    labels: list[str]
    datasets: list[ChartDataset]


class DashboardMetrics(TypedDict):
    kpi: list[KpiCard]
    top_groups_chart: ChartData
    payments_chart: ChartData


def _format_rubles(amount_kopecks: int) -> str:
    return f"{amount_kopecks / 100:,.0f} ₽".replace(",", " ")


def _kpi_cards(month_start: date) -> list[KpiCard]:
    revenue: int = (
        Transaction.objects.filter(
            status=TransactionStatus.SUCCEEDED,
            created_at__date__gte=month_start,
        ).aggregate(total=Sum("amount"))["total"]
        or 0
    )
    active_students: int = (
        Student.objects.filter(enrollments__status=EnrollmentStatus.ENROLLED)
        .distinct()
        .count()
    )
    return [
        {"title": "Выручка за месяц", "metric": _format_rubles(revenue)},
        {"title": "Активных учеников", "metric": str(active_students)},
    ]


def _top_groups_chart() -> ChartData:
    # ПОЧЕМУ: HELD не считаем — это 15-минутная бронь до оплаты,
    # а не показатель популярности группы
    rows = (
        Enrollment.objects.filter(status=EnrollmentStatus.ENROLLED)
        .values("schedule_id", "schedule__activity__name", "schedule__group_name")
        .annotate(enrollments_count=Count("id"))
        .order_by("-enrollments_count")[:5]
    )
    labels: list[str] = [
        row["schedule__group_name"] or row["schedule__activity__name"] or "—"
        for row in rows
    ]
    data: list[float] = [float(row["enrollments_count"]) for row in rows]
    return {"labels": labels, "datasets": [{"label": "Записей", "data": data}]}


def _payments_chart(today: date) -> ChartData:
    since = today - timedelta(days=29)
    rows = (
        Transaction.objects.filter(
            status=TransactionStatus.SUCCEEDED,
            created_at__date__gte=since,
        )
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(total=Sum("amount"))
        .order_by("day")
    )
    totals: dict[date, int] = {row["day"]: row["total"] for row in rows}
    days: list[date] = [since + timedelta(days=offset) for offset in range(30)]
    return {
        "labels": [day.strftime("%d.%m") for day in days],
        "datasets": [
            {
                "label": "Платежи, ₽",
                "data": [totals.get(day, 0) / 100 for day in days],
            }
        ],
    }


def build_dashboard_metrics() -> DashboardMetrics:
    today = timezone.localdate()
    month_start = today.replace(day=1)
    return {
        "kpi": _kpi_cards(month_start),
        "top_groups_chart": _top_groups_chart(),
        "payments_chart": _payments_chart(today),
    }


def dashboard_callback(request: HttpRequest, context: dict[str, Any]) -> dict[str, Any]:
    # ПОЧЕМУ: cache-aside — агрегации бьют БД на каждый заход в админку,
    # отдаём из Redis (default cache) с TTL
    # TODO: пересчёт вынести в периодическую Taskiq-задачу, которая кладёт
    # готовые метрики в этот же ключ — тогда промах кэша перестанет считать
    # агрегации в HTTP-потоке админки
    metrics: DashboardMetrics | None = cache.get(DASHBOARD_CACHE_KEY)
    if metrics is None:
        metrics = build_dashboard_metrics()
        cache.set(DASHBOARD_CACHE_KEY, metrics, DASHBOARD_CACHE_TTL)

    context.update(
        {
            "kpi": metrics["kpi"],
            # ПОЧЕМУ: компоненты chart в Unfold принимают data строго
            # JSON-строкой, dict молча рендерится пустым графиком
            "top_groups_chart": json.dumps(metrics["top_groups_chart"]),
            "payments_chart": json.dumps(metrics["payments_chart"]),
        }
    )
    return context
