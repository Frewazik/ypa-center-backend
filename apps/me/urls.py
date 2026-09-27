from __future__ import annotations

from django.urls import path

from apps.me.views import (
    BookingListView,
    ChildCreateView,
    ChildDetailView,
    DepositBalanceView,
    DepositEntryListView,
    ProfileView,
    SubscriptionListView,
    TrialListView,
    UpcomingFeedView,
)

app_name = "me"

urlpatterns = [
    path("profile/", ProfileView.as_view(), name="profile"),
    path("children/", ChildCreateView.as_view(), name="child-create"),
    path("children/<int:pk>/", ChildDetailView.as_view(), name="child-detail"),
    path("subscriptions/", SubscriptionListView.as_view(), name="subscriptions"),
    path("trials/", TrialListView.as_view(), name="trials"),
    path("bookings/", BookingListView.as_view(), name="bookings"),
    path("deposit/", DepositBalanceView.as_view(), name="deposit"),
    path("deposit/entries/", DepositEntryListView.as_view(), name="deposit-entries"),
    path("upcoming/", UpcomingFeedView.as_view(), name="upcoming"),
]
