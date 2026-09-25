from __future__ import annotations

import pytest
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import status
from rest_framework.exceptions import (
    APIException,
    NotAuthenticated,
    NotFound,
    ParseError,
    PermissionDenied,
    Throttled,
    ValidationError,
)
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from apps.billing.views import (
    EnrollmentConflict,
    IdempotencyKeyConflict,
    NoSeatsConflict,
    TrialLimitConflict,
)
from apps.core.exceptions import problem_detail_exception_handler


def _handle(exc: Exception, **headers: str) -> dict[str, object]:
    request = Request(APIRequestFactory().get("/", **headers))
    response = problem_detail_exception_handler(exc, {"request": request})
    assert response is not None
    return dict(response.data)


class TestProblemCode:
    @pytest.mark.parametrize(
        ("exc", "code"),
        [
            (NoSeatsConflict(), "NO_AVAILABLE_SEATS"),
            (TrialLimitConflict(), "TRIAL_LIMIT_EXCEEDED"),
            (EnrollmentConflict(), "STUDENT_ALREADY_ENROLLED"),
            (IdempotencyKeyConflict(), "IDEMPOTENCY_KEY_REUSED"),
        ],
    )
    def test_business_conflicts_carry_their_code(
        self, exc: APIException, code: str
    ) -> None:
        # ПОЧЕМУ: несколько 409 различаются только кодом — фронт делает по нему switch
        data = _handle(exc)

        assert data["status"] == status.HTTP_409_CONFLICT
        assert data["code"] == code

    @pytest.mark.parametrize(
        ("exc", "code"),
        [
            (ParseError(), "MALFORMED_REQUEST"),
            (NotAuthenticated(), "AUTH_REQUIRED"),
            (PermissionDenied(), "FORBIDDEN_RESOURCE"),
            (NotFound(), "NOT_FOUND"),
            (Throttled(wait=5), "RATE_LIMITED"),
            (Http404(), "NOT_FOUND"),
            (DjangoPermissionDenied(), "FORBIDDEN_RESOURCE"),
        ],
    )
    def test_builtin_errors_map_to_catalog(self, exc: Exception, code: str) -> None:
        assert _handle(exc)["code"] == code

    def test_custom_code_on_raise_wins_over_default(self) -> None:
        assert _handle(NotFound(code="SLOT_GONE"))["code"] == "SLOT_GONE"

    def test_validation_error_has_single_catalog_code(self) -> None:
        exc = ValidationError({"email": ["bad"]}, code="IDEMPOTENCY_KEY_REQUIRED")

        data = _handle(exc)

        assert data["status"] == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert data["code"] == "VALIDATION_ERROR"

    def test_code_is_top_level_next_to_extensions(self) -> None:
        data = _handle(NoSeatsConflict(), HTTP_X_REQUEST_ID="req-1")

        assert data["code"] == "NO_AVAILABLE_SEATS"
        assert data["extensions"] == {"request_id": "req-1"}

    def test_unhandled_exception_is_internal_server_error(self) -> None:
        data = _handle(RuntimeError("boom"))

        assert data["status"] == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert data["code"] == "INTERNAL_SERVER_ERROR"
