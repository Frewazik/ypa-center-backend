from __future__ import annotations

from django.urls import path

from apps.public_forms.views import CallbackRequestCreateView, FeedbackRequestCreateView

app_name = "public_forms"

urlpatterns = [
    path("callback/", CallbackRequestCreateView.as_view(), name="callback-create"),
    path("feedback/", FeedbackRequestCreateView.as_view(), name="feedback-create"),
]
