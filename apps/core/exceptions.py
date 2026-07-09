import logging
from typing import TypeAlias, TypedDict, cast

from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)

ErrorData: TypeAlias = dict[str, "ErrorData"] | list["ErrorData"] | str


class InvalidParam(TypedDict):
    name: str
    reason: str


def _get_invalid_params(
    error_data: ErrorData, parent_key: str = ""
) -> list[InvalidParam]:
    params: list[InvalidParam] = []

    if isinstance(error_data, dict):
        for key, value in error_data.items():
            prefix = f"{parent_key}.{key}" if parent_key else str(key)
            params.extend(_get_invalid_params(value, prefix))
    elif isinstance(error_data, list):
        for index, item in enumerate(error_data):
            if isinstance(item, str):
                params.extend(_get_invalid_params(item, parent_key))
            else:
                prefix = f"{parent_key}[{index}]" if parent_key else str(index)
                params.extend(_get_invalid_params(item, prefix))
    else:
        params.append(
            {
                "name": parent_key or cast(str, api_settings.NON_FIELD_ERRORS_KEY),
                "reason": str(error_data),
            }
        )

    return params


def problem_detail_exception_handler(
    exc: Exception, context: dict[str, object]
) -> Response | None:
    response = exception_handler(exc, context)

    # ПОЧЕМУ: Спецификация контракта требует отдавать 422 UNPROCESSABLE_ENTITY на ошибки валидации данных вместо дефолтного 400
    if response is not None and isinstance(exc, ValidationError):
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY

    request = context.get("request")
    request_id = request.META.get("HTTP_X_REQUEST_ID") if request else None

    if response is None:
        logger.error("Необработанное исключение API: %s", str(exc), exc_info=True)

        data: dict[str, object] = {
            "type": "urn:problem-type:internal-server-error",
            "title": "Internal Server Error",
            "status": status.HTTP_500_INTERNAL_SERVER_ERROR,
            "detail": _("Внутренняя ошибка сервера. Инцидент зафиксирован."),
        }

        if request_id:
            data["extensions"] = {"request_id": request_id}

        return Response(
            data=data,
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    payload: dict[str, object] = {
        "type": f"urn:problem-type:{exc.__class__.__name__.lower()}",
        "title": exc.__class__.__name__,
        "status": response.status_code,
    }

    if isinstance(response.data, dict):
        payload["detail"] = response.data.get("detail", str(exc))
    else:
        payload["detail"] = str(exc)

    extensions: dict[str, object] = {}
    if request_id:
        extensions["request_id"] = request_id

    if isinstance(exc, ValidationError):
        payload["title"] = "Validation Error"
        payload["detail"] = _("Ошибка валидации входных данных.")
        extensions["invalid_params"] = _get_invalid_params(
            cast(ErrorData, response.data)
        )

    if extensions:
        payload["extensions"] = extensions

    response.data = payload
    return response
