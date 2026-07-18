from __future__ import annotations

from django.urls import path

from apps.events.views import EventRegistrationCreateView

app_name = "events"

urlpatterns = [
    path(
        "events/<int:event_id>/register/",
        EventRegistrationCreateView.as_view(),
        name="event-register",
    ),
]
