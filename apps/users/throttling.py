from __future__ import annotations

from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from apps.core.throttling import ClientIPRateThrottle


# ПОЧЕМУ: не AnonRateThrottle — он пропускает запросы с валидным токеном,
# и с чужим/своим токеном IP-лимит на вход не действовал
class OTPRequestPerIPThrottle(ClientIPRateThrottle):
    scope = "otp_request_ip"


class OTPRequestPerEmailThrottle(SimpleRateThrottle):
    scope = "otp_request_email"

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        email = request.data.get("email")
        if not isinstance(email, str) or not email.strip():
            # ПОЧЕМУ: возврат None отключает правило для запроса. Пустые email упадут дальше на валидации сериализатора.
            return None

        return self.cache_format % {
            "scope": self.scope,
            "ident": email.strip().lower(),
        }


class OTPVerifyPerIPThrottle(ClientIPRateThrottle):
    scope = "otp_verify_ip"


class AuthTokenRefreshThrottle(SimpleRateThrottle):
    scope = "auth_token_refresh"

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


class AuthLogoutThrottle(SimpleRateThrottle):
    scope = "auth_logout"

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }
