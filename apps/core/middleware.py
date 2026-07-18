import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse
from django.utils.decorators import sync_and_async_middleware


@sync_and_async_middleware
class RequestIDMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = request.META.get("HTTP_X_REQUEST_ID") or uuid.uuid4().hex

        request.META["HTTP_X_REQUEST_ID"] = request_id

        # FIXME: Техдолг. Для production-ready логирования нужно биндить request_id
        # в контекстные переменные (например, structlog.contextvars.bind_contextvars или кастомный threading.local),
        # чтобы каждый вызов logger.info/error автоматически подтягивал этот ID в логи.

        response = self.get_response(request)
        response["X-Request-ID"] = request_id

        return response
