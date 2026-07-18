from __future__ import annotations

from django.urls import path

from apps.me.views import (
    ChildCreateView,
    ChildUpdateView,
    ProfileView,
    SubscriptionListView,
    UpcomingFeedView,
)

app_name = "me"

urlpatterns = [
    path("profile/", ProfileView.as_view(), name="profile"),
    path("children/", ChildCreateView.as_view(), name="child-create"),
    path("children/<int:pk>/", ChildUpdateView.as_view(), name="child-update"),
    path("subscriptions/", SubscriptionListView.as_view(), name="subscriptions"),
    path("upcoming/", UpcomingFeedView.as_view(), name="upcoming"),
]
