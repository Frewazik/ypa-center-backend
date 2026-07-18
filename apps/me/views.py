from __future__ import annotations

from typing import cast

from django.db.models import QuerySet
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.me.serializers import (
    ChildSerializer,
    ProfileSerializer,
    SubscriptionViewSerializer,
    UpcomingItemSerializer,
)
from apps.me.services import (
    UPCOMING_DEFAULT_WEEKS,
    UPCOMING_MAX_WEEKS,
    build_upcoming_feed,
    create_child,
    list_parent_subscriptions,
)
from apps.users.models import Parent, Student


def _current_parent(request: Request) -> Parent:
    return cast(Parent, request.user)


@extend_schema(
    operation_id="me_profile",
    summary="Профиль родителя с детьми",
    responses=ProfileSerializer,
)
class ProfileView(generics.RetrieveUpdateAPIView[Parent]):
    serializer_class = ProfileSerializer
    http_method_names = ("get", "patch", "options")

    def get_object(self) -> Parent:
        return _current_parent(self.request)


@extend_schema(
    operation_id="me_child_create",
    summary="Добавить ребёнка",
    request=ChildSerializer,
    responses={status.HTTP_201_CREATED: ChildSerializer},
)
class ChildCreateView(APIView):
    def post(self, request: Request) -> Response:
        serializer = ChildSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        child = create_child(
            _current_parent(request),
            cast("dict[str, object]", serializer.validated_data),
        )
        return Response(ChildSerializer(child).data, status=status.HTTP_201_CREATED)


@extend_schema(
    operation_id="me_child_update",
    summary="Изменить данные ребёнка",
    responses=ChildSerializer,
)
class ChildUpdateView(generics.UpdateAPIView[Student]):
    serializer_class = ChildSerializer
    http_method_names = ("patch", "options")

    def get_queryset(self) -> QuerySet[Student]:
        # Чужой ребёнок неотличим от несуществующего - 404
        return Student.objects.filter(parent=_current_parent(self.request))


@extend_schema(
    operation_id="me_subscriptions",
    summary="Мои абонементы с балансом по слотам",
    responses=SubscriptionViewSerializer(many=True),
)
class SubscriptionListView(APIView):
    def get(self, request: Request) -> Response:
        views = list_parent_subscriptions(_current_parent(request))
        return Response(SubscriptionViewSerializer(views, many=True).data)


@extend_schema(
    operation_id="me_upcoming",
    summary="Лента ближайших активностей (занятия + события)",
    parameters=[
        OpenApiParameter(name="weeks", type=int, required=False),
        OpenApiParameter(name="child_id", type=int, required=False),
    ],
    responses=UpcomingItemSerializer(many=True),
)
class UpcomingFeedView(APIView):
    def get(self, request: Request) -> Response:
        weeks = _positive_int(request.query_params.get("weeks"), "weeks")
        child_id = _positive_int(request.query_params.get("child_id"), "child_id")
        items = build_upcoming_feed(
            _current_parent(request),
            weeks=min(weeks or UPCOMING_DEFAULT_WEEKS, UPCOMING_MAX_WEEKS),
            child_id=child_id,
        )
        return Response(UpcomingItemSerializer(items, many=True).data)


def _positive_int(raw: str | None, field: str) -> int | None:
    if raw is None or raw == "":
        return None
    if not raw.isdigit() or int(raw) < 1:
        raise ValidationError(
            {field: ["Ожидается целое число больше нуля."]},
            code="VALIDATION_ERROR",
        )
    return int(raw)
