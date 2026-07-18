from __future__ import annotations

from django.urls import path

from apps.public_api.views import (
    ActivityDetailView,
    PopularActivitiesView,
    PublicActivityListView,
    PublicEventListView,
    PublicGalleryListView,
    PublicPlanListView,
    PublicTeacherListView,
)

app_name = "public_api"

urlpatterns = [
    path("activities/", PublicActivityListView.as_view(), name="activities-list"),
    path(
        "activities/popular/",
        PopularActivitiesView.as_view(),
        name="activities-popular",
    ),
    path("activities/<int:pk>/", ActivityDetailView.as_view(), name="activity-detail"),
    path("teachers/", PublicTeacherListView.as_view(), name="teachers-list"),
    path("gallery/", PublicGalleryListView.as_view(), name="gallery-list"),
    path("events/", PublicEventListView.as_view(), name="events-list"),
    path("plans/", PublicPlanListView.as_view(), name="plans-list"),
]
