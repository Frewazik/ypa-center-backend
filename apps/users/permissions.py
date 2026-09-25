from __future__ import annotations

from typing import TYPE_CHECKING

from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

from apps.users.models import Parent

if TYPE_CHECKING:
    # ПОЧЕМУ: модуль указан в DEFAULT_PERMISSION_CLASSES, и DRF импортирует
    # его посреди загрузки rest_framework.views — рантайм-импорт дал бы цикл
    from rest_framework.request import Request
    from rest_framework.views import APIView


class ProfileIncomplete(PermissionDenied):
    # ПОЧЕМУ: отдельный класс, а не PermissionDenied с message — обработчик
    # ошибок строит `type` из имени класса, и фронт отличает этот 403
    # от «чужого ребёнка» уже сейчас: urn:problem-type:profileincomplete.
    # default_code станет полем `code`, когда в обработчик вернётся code
    default_detail = (
        "Заполните анкету: ФИО, телефон, «откуда вы о нас узнали» "
        "и согласие на обработку персональных данных."
    )
    default_code = "PROFILE_INCOMPLETE"


class IsProfileCompleted(BasePermission):
    """Пускает только родителя с заполненной анкетой.

    Стоит в DEFAULT_PERMISSION_CLASSES: новые ручки закрыты по умолчанию,
    анкета (профиль, дети) открывается явным `permission_classes`.
    """

    def has_permission(self, request: Request, view: APIView) -> bool:
        user = request.user
        if not isinstance(user, Parent) or not user.is_authenticated:
            # ПОЧЕМУ: гостя отсекает IsAuthenticated; при отказе без токена
            # DRF сам отдаёт 401, а не 403
            return False
        if not user.is_profile_completed:
            raise ProfileIncomplete()
        return True
