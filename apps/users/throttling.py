from __future__ import annotations

from rest_framework.request import Request
from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle
from rest_framework.views import APIView


class OTPRequestPerIPThrottle(AnonRateThrottle):
    scope = "otp_request_ip"
    rate = "5/hour"


class OTPRequestPerEmailThrottle(SimpleRateThrottle):
    scope = "otp_request_email"
    rate = "5/hour"

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        email = request.data.get("email")
        if not isinstance(email, str) or not email.strip():
            # ПОЧЕМУ: возврат None отключает правило для запроса. Пустые email упадут дальше на валидации сериализатора.
            return None

        return self.cache_format % {
            "scope": self.scope,
            "ident": email.strip().lower(),
        }


class OTPVerifyPerIPThrottle(AnonRateThrottle):
    scope = "otp_verify_ip"
    rate = "10/min"
