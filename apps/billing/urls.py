from __future__ import annotations

from django.urls import path

from apps.billing.views import CheckoutSubscriptionView, YookassaWebhookView

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
        "webhooks/yookassa",
        YookassaWebhookView.as_view(),
        name="yookassa-webhook",
    ),
]
