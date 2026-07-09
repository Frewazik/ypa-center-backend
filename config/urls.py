from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.users.views import OTPRequestView, OTPVerifyView

urlpatterns = [
    path("admin/", admin.site.urls),
    # OpenAPI схемы
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/schema/swagger-ui/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    # Эндпоинты (Домен Users)
    path("api/v1/auth/otp/request/", OTPRequestView.as_view(), name="otp_request"),
    path("api/v1/auth/otp/verify/", OTPVerifyView.as_view(), name="otp_verify"),
    path("api/v1/public/", include("apps.schedule.urls")),
]
