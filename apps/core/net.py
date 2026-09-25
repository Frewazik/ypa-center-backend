from __future__ import annotations

from django.http import HttpRequest
from rest_framework.request import Request
from rest_framework.settings import api_settings


def client_ip(request: HttpRequest | Request) -> str:
    """Реальный IP клиента — единое правило для лимитов, капчи и вебхука.

    Перед приложением стоит `NUM_PROXIES` своих прокси (settings:
    TRUSTED_PROXY_COUNT). Каждый свой прокси дописывает в конец
    X-Forwarded-For адрес, от которого получил запрос, поэтому доверять
    можно только N-му адресу с конца. Всё левее написал клиент сам.

    Пример при одном прокси (Caddy): клиент 203.0.113.7 прислал
    ``X-Forwarded-For: 1.2.3.4`` → до Django доходит ``1.2.3.4, 203.0.113.7``
    (или просто ``203.0.113.7``, если прокси затирает чужой заголовок) →
    берём 203.0.113.7, подделка 1.2.3.4 игнорируется. Правило одинаково
    работает для обоих вариантов поведения прокси.
    """
    remote_addr: str = request.META.get("REMOTE_ADDR", "")
    trusted_proxies: int = api_settings.NUM_PROXIES or 0
    if trusted_proxies == 0:
        # ПОЧЕМУ: без своего прокси X-Forwarded-For целиком пишет клиент —
        # верить можно только адресу TCP-соединения
        return remote_addr

    forwarded: str = request.META.get("HTTP_X_FORWARDED_FOR", "")
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    if not hops:
        return remote_addr
    return hops[-min(trusted_proxies, len(hops))]
