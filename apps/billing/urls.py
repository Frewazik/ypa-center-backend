from __future__ import annotations

from django.urls import path

from apps.billing.views import (
    CheckoutSubscriptionView,
    CheckoutTransactionView,
    CheckoutTrialView,
    EventCheckoutTransactionView,
    YookassaWebhookView,
)

app_name = "billing"

# !!!: пути намеренно указаны без слеша на конце (trailing slash)
# если провайдер дернет URL без слеша, а в паттерне он будет, Django отдаст 301 редирект
# при 301 редиректе POST-запросы от вебхуков необратимо теряют тело (payload)

urlpatterns = [
    path(
        "checkout/subscription",
        CheckoutSubscriptionView.as_view(),
        name="checkout-subscription",
    ),
    path(
        "checkout/trial",
        CheckoutTrialView.as_view(),
        name="checkout-trial",
    ),
    path(
        "checkout/transactions/<str:transaction_id>",
        CheckoutTransactionView.as_view(),
        name="checkout-transaction",
    ),
    # ПОЧЕМУ со слешем: публичная зона /public/* у нас со слешем, а GET
    # без тела редирект APPEND_SLASH не ломает
    path(
        "public/events/payments/<str:transaction_id>/",
        EventCheckoutTransactionView.as_view(),
        name="event-payment-status",
    ),
    path(
        "webhooks/yookassa",
        YookassaWebhookView.as_view(),
        name="yookassa-webhook",
    ),
]
