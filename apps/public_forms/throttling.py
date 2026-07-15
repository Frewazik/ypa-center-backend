from __future__ import annotations

from ipware import get_client_ip
from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView


class PublicFormIPThrottle(SimpleRateThrottle):
    rate = "3/min"

    def get_cache_key(self, request: Request, view: APIView) -> str:
        # ПОЧЕМУ: базовый get_ident за прокси схлопывает всех пользователей
        # в один IP прокси и спуфится через X-Forwarded-For
        client_ip, _ = get_client_ip(request)
        ident = client_ip or self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class CallbackIPThrottle(PublicFormIPThrottle):
    scope = "public_forms_callback"


class FeedbackIPThrottle(PublicFormIPThrottle):
    scope = "public_forms_feedback"
