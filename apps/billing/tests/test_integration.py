# Сквозной тест продакшен-среза checkout без единого мока собственного кода:
# реальный JWT (simplejwt) -> реальный CheckoutSubscriptionView по URL ->
# реальный DjangoSchedulePort из дефолтного BILLING_SCHEDULE_PORT_CLASS ->
# реальный YookassaHttpGateway. Подменяется ровно одна вещь — сетевой
# транспорт httpx (httpx.MockTransport), то есть провод до api.yookassa.ru.

from __future__ import annotations

import json
import uuid
from functools import partial

import httpx
import pytest
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.billing.adapters import YookassaHttpGateway, YookassaSettings
from apps.billing.models import (
    Enrollment,
    EnrollmentStatus,
    Subscription,
    SubscriptionStatus,
    Transaction,
    TransactionStatus,
)
from apps.billing.ports import resolve_schedule_port
from apps.schedule.ports import DjangoSchedulePort
from apps.schedule.tests.factories import (
    ScheduleFactory,
    StudentFactory,
    SubscriptionPlanFactory,
)

CHECKOUT_URL = "/api/v1/checkout/subscription"


def _yookassa_transport(seen_requests: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "2e8f3c1a-000f-5000-9000-1db2a1a1e0c1",
                "status": "pending",
                "amount": payload["amount"],
                "confirmation": {
                    "type": "redirect",
                    "confirmation_url": (
                        "https://yookassa.ru/checkout/confirm/2e8f3c1a"
                    ),
                },
                "metadata": payload["metadata"],
            },
        )

    return httpx.MockTransport(handler)


@pytest.mark.django_db
class TestCheckoutEndToEnd:
    def test_jwt_checkout_creates_pending_transaction_and_held_seat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        schedule = ScheduleFactory(max_capacity=6)
        student = StudentFactory()
        parent = student.parent
        plan = SubscriptionPlanFactory(slots_count=1, price=700_000)

        # Дефолтная настройка BILLING_SCHEDULE_PORT_CLASS обязана резолвить
        # реальную реализацию порта — ровно этот шов был сломан до починки
        assert isinstance(resolve_schedule_port(), DjangoSchedulePort)

        seen_requests: list[httpx.Request] = []
        gateway_settings = YookassaSettings(
            shop_id="test-shop",
            secret_key="test-secret",
            return_url="https://ulitsa-radosti.ru/checkout/result",
        )
        monkeypatch.setattr(
            "apps.billing.views.YookassaHttpGateway",
            partial(
                YookassaHttpGateway,
                settings=gateway_settings,
                transport=_yookassa_transport(seen_requests),
            ),
        )

        refresh = RefreshToken.for_user(parent)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

        response = client.post(
            CHECKOUT_URL,
            {
                "plan_id": plan.pk,
                "student_id": student.pk,
                "slot_ids": [schedule.pk],
            },
            format="json",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )

        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert response.data["status"] == "PENDING_PAYMENT"
        assert (
            response.data["payment_url"]
            == "https://yookassa.ru/checkout/confirm/2e8f3c1a"
        )

        tx = Transaction.objects.get()
        assert str(tx.pk) == response.data["transaction_id"]
        assert tx.status == TransactionStatus.PENDING
        assert tx.parent_id == parent.pk
        assert tx.amount == plan.price
        assert tx.external_id == "2e8f3c1a-000f-5000-9000-1db2a1a1e0c1"
        assert tx.selected_slot_ids == [schedule.pk]

        subscription = Subscription.objects.get()
        assert subscription.status == SubscriptionStatus.PENDING
        assert subscription.parent_id == parent.pk

        enrollment = Enrollment.objects.get()
        assert enrollment.status == EnrollmentStatus.HELD
        assert enrollment.student_id == student.pk
        assert enrollment.schedule_id == schedule.pk

        # Контракт запроса к ЮКассе: Idempotence-Key, metadata.transaction_id,
        # сумма в рублях с копейками и redirect-подтверждение
        (yookassa_request,) = seen_requests
        assert yookassa_request.url.path.endswith("/v3/payments")
        assert yookassa_request.headers["Idempotence-Key"] == f"payment-{tx.pk}"
        body = json.loads(yookassa_request.content)
        assert body["metadata"]["transaction_id"] == str(tx.pk)
        assert body["amount"] == {"value": "7000.00", "currency": "RUB"}
        assert body["confirmation"] == {
            "type": "redirect",
            "return_url": "https://ulitsa-radosti.ru/checkout/result",
        }
