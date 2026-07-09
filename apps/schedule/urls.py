from __future__ import annotations

from django.urls import path

from apps.schedule.views import PublicScheduleView

app_name = "schedule"

urlpatterns = [
    path("schedule/", PublicScheduleView.as_view(), name="public-schedule"),
]
