from __future__ import annotations

from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from apps.core.net import client_ip


class ClientIPRateThrottle(SimpleRateThrottle):
    def get_cache_key(self, request: Request, view: APIView) -> str:
        # ПОЧЕМУ: IP берём по общему правилу apps.core.net — только адрес,
        # который дописал свой прокси; подделанный клиентом X-Forwarded-For
        # не даёт ни обойти лимит, ни «подставить» чужой IP
        return self.cache_format % {"scope": self.scope, "ident": client_ip(request)}
