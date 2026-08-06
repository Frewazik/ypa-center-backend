from __future__ import annotations

from django.urls import path

from apps.me.views import (
    ChildCreateView,
    ChildUpdateView,
    ProfileView,
    SubscriptionListView,
    TrialListView,
    UpcomingFeedView,
)

app_name = "me"

urlpatterns = [
    path("profile/", ProfileView.as_view(), name="profile"),
    path("children/", ChildCreateView.as_view(), name="child-create"),
    path("children/<int:pk>/", ChildUpdateView.as_view(), name="child-update"),
    path("subscriptions/", SubscriptionListView.as_view(), name="subscriptions"),
    path("trials/", TrialListView.as_view(), name="trials"),
    path("upcoming/", UpcomingFeedView.as_view(), name="upcoming"),
]
