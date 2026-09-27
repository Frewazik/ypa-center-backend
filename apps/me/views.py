from __future__ import annotations

from typing import cast

from django.db.models import QuerySet
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import generics, status
from rest_framework.exceptions import APIException
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.views import APIView

from apps.core.pagination import (
    LIMIT_OFFSET_PARAMETERS,
    paginated_response,
    parse_int_param,
)
from apps.me.serializers import (
    ActiveEnrollmentSerializer,
    ChildSerializer,
    DepositBalanceSerializer,
    DepositEntryViewSerializer,
    ProfileSerializer,
    SubscriptionViewSerializer,
    TrialViewSerializer,
    UpcomingItemSerializer,
)
from apps.me.services import (
    UPCOMING_DEFAULT_WEEKS,
    UPCOMING_MAX_WEEKS,
    ActiveEnrollmentView,
    ChildHasActiveEnrollmentsError,
    archive_child,
    build_deposit_entry_views,
    build_subscription_views,
    build_trial_views,
    build_upcoming_feed,
    create_child,
    get_parent_deposit_balance,
    parent_deposit_entries_query,
    parent_subscriptions_query,
    parent_trials_query,
    update_child,
)
from apps.users.models import Parent, Student


def _current_parent(request: Request) -> Parent:
    return cast(Parent, request.user)


# ПОЧЕМУ: по умолчанию ЛК закрыт до заполнения анкеты (IsProfileCompleted
# в settings). Профиль и дети — это и есть анкета, им нужен только вход
_ONBOARDING_PERMISSIONS = [IsAuthenticated]


@extend_schema(
    operation_id="me_profile",
    summary="Профиль родителя с детьми",
    responses=ProfileSerializer,
)
class ProfileView(generics.RetrieveUpdateAPIView[Parent]):
    permission_classes = _ONBOARDING_PERMISSIONS
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
    permission_classes = _ONBOARDING_PERMISSIONS

    def post(self, request: Request) -> Response:
        serializer = ChildSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        child = create_child(
            _current_parent(request),
            cast("dict[str, object]", serializer.validated_data),
        )
        return Response(ChildSerializer(child).data, status=status.HTTP_201_CREATED)


class ChildHasActiveEnrollmentsConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = (
        "У ребёнка есть действующий абонемент, неоплаченная бронь или "
        "предстоящее пробное. Удалить можно после их окончания."
    )
    default_code = "CHILD_HAS_ACTIVE_ENROLLMENTS"

    def __init__(self, enrollments: list[ActiveEnrollmentView]) -> None:
        super().__init__()
        # Попадает в extensions ответа — фронт показывает, что именно мешает
        self.extensions = {
            "active_enrollments": ActiveEnrollmentSerializer(
                enrollments, many=True
            ).data
        }


@extend_schema_view(
    patch=extend_schema(
        operation_id="me_child_update",
        summary="Изменить данные ребёнка",
        responses=ChildSerializer,
    ),
    delete=extend_schema(
        operation_id="me_child_delete",
        summary="Удалить ребёнка (архивация)",
        description=(
            "Мягкое удаление: ребёнок пропадает из профиля и чекаута, история "
            "покупок и посещений сохраняется. 409 CHILD_HAS_ACTIVE_ENROLLMENTS, "
            "пока есть действующий абонемент, неоплаченная бронь или предстоящее "
            "пробное — список в extensions.active_enrollments."
        ),
        responses={
            status.HTTP_204_NO_CONTENT: OpenApiResponse(description="Удалён"),
            status.HTTP_404_NOT_FOUND: OpenApiResponse(
                description="Чужой, несуществующий или уже удалённый ребёнок"
            ),
            status.HTTP_409_CONFLICT: OpenApiResponse(
                description="CHILD_HAS_ACTIVE_ENROLLMENTS"
            ),
        },
    ),
)
class ChildDetailView(generics.UpdateAPIView[Student]):
    permission_classes = _ONBOARDING_PERMISSIONS
    serializer_class = ChildSerializer
    http_method_names = ("patch", "delete", "options")

    def get_queryset(self) -> QuerySet[Student]:
        # Чужой ребёнок неотличим от несуществующего - 404; удалённый - тоже
        return Student.objects.active().filter(parent=_current_parent(self.request))

    def perform_update(self, serializer: BaseSerializer[Student]) -> None:
        serializer.instance = update_child(
            cast(Student, serializer.instance),
            cast("dict[str, object]", serializer.validated_data),
        )

    def delete(self, request: Request, pk: int) -> Response:
        try:
            archive_child(_current_parent(request), pk)
        except ChildHasActiveEnrollmentsError as exc:
            raise ChildHasActiveEnrollmentsConflict(exc.enrollments) from exc
        return Response(status=status.HTTP_204_NO_CONTENT)


_PAGINATION_NOTE = (
    " Без limit — весь список массивом; с ?limit=N&offset=M — конверт "
    "{count, next, previous, results}, где results — те же элементы."
)


@extend_schema(
    operation_id="me_subscriptions",
    summary="Мои абонементы: действующие и история",
    description=(
        "Только оплаченные: ACTIVE (сверху) и EXPIRED, внутри — новые сверху. "
        "У истёкшего — все купленные слоты, остаток 0 (он ушёл на депозит)."
        + _PAGINATION_NOTE
    ),
    parameters=LIMIT_OFFSET_PARAMETERS,
    responses=SubscriptionViewSerializer(many=True),
)
class SubscriptionListView(APIView):
    def get(self, request: Request) -> Response:
        return paginated_response(
            request,
            parent_subscriptions_query(_current_parent(request)),
            lambda page: (
                SubscriptionViewSerializer(
                    build_subscription_views(page), many=True
                ).data
            ),
        )


@extend_schema(
    operation_id="me_trials",
    summary="Пробные занятия детей родителя",
    description="Новые по дате пробного сверху." + _PAGINATION_NOTE,
    parameters=LIMIT_OFFSET_PARAMETERS,
    responses=TrialViewSerializer(many=True),
)
class TrialListView(APIView):
    def get(self, request: Request) -> Response:
        return paginated_response(
            request,
            parent_trials_query(_current_parent(request)),
            lambda page: TrialViewSerializer(build_trial_views(page), many=True).data,
        )


@extend_schema(
    operation_id="me_deposit",
    summary="Баланс депозита родителя",
    description=(
        "Баланс в копейках. Нет депозита — 0. Нужен чекауту, чтобы решить, "
        "предлагать ли оплату с депозита (use_deposit)."
    ),
    responses=DepositBalanceSerializer,
)
class DepositBalanceView(APIView):
    def get(self, request: Request) -> Response:
        balance = get_parent_deposit_balance(_current_parent(request))
        return Response(DepositBalanceSerializer({"balance": balance}).data)


@extend_schema(
    operation_id="me_deposit_entries",
    summary="История движений депозита",
    description=(
        "Новые сверху. amount со знаком: плюс — начисление, минус — списание."
        + _PAGINATION_NOTE
    ),
    parameters=LIMIT_OFFSET_PARAMETERS,
    responses=DepositEntryViewSerializer(many=True),
)
class DepositEntryListView(APIView):
    def get(self, request: Request) -> Response:
        return paginated_response(
            request,
            parent_deposit_entries_query(_current_parent(request)),
            lambda page: (
                DepositEntryViewSerializer(
                    build_deposit_entry_views(page), many=True
                ).data
            ),
        )


@extend_schema(
    operation_id="me_upcoming",
    summary="Лента ближайших активностей (занятия + события)",
    description=(
        "Хронологически, ближайшие сверху. Горизонт weeks (по умолчанию 4, "
        "максимум 8)." + _PAGINATION_NOTE
    ),
    parameters=[
        OpenApiParameter(name="weeks", type=int, required=False),
        OpenApiParameter(name="child_id", type=int, required=False),
        *LIMIT_OFFSET_PARAMETERS,
    ],
    responses=UpcomingItemSerializer(many=True),
)
class UpcomingFeedView(APIView):
    def get(self, request: Request) -> Response:
        params = request.query_params
        weeks = parse_int_param(params.get("weeks"), "weeks", minimum=1)
        child_id = parse_int_param(params.get("child_id"), "child_id", minimum=1)
        # ПОЧЕМУ срез в Python: будущих занятий нет в БД, лента собирается
        # циклом по датам. Горизонт ≤ 8 недель — это десятки строк
        items = build_upcoming_feed(
            _current_parent(request),
            weeks=min(weeks or UPCOMING_DEFAULT_WEEKS, UPCOMING_MAX_WEEKS),
            child_id=child_id,
        )
        return paginated_response(
            request,
            items,
            lambda page: UpcomingItemSerializer(page, many=True).data,
        )
