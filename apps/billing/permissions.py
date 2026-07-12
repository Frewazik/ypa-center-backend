from __future__ import annotations

from ipaddress import ip_address, ip_network

from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

_ALLOWED_NETWORKS = tuple(
    ip_network(cidr)
    for cidr in (
        "185.71.76.0/27",
        "185.71.77.0/27",
        "77.75.153.0/25",
        "77.75.154.128/25",
        "2a02:5180::/32",
    )
)


class YookassaIPAllowlist(BasePermission):
    # !!! Читаем строго REMOTE_ADDR для защиты от спуфинга

    # Доверенный reverse-proxy (nginx/traefik) ОБЯЗАН транслировать реальный IP
    # клиента в REMOTE_ADDR на уровне WSGI/ASGI

    # Парсить X-Forwarded-For внутри приложения напрямую запрещено,
    # заголовок легко подделывается

    message = "Источник запроса не входит в список разрешённых."

    def has_permission(self, request: Request, view: APIView) -> bool:
        raw_addr = request.META.get("REMOTE_ADDR", "")
        try:
            addr = ip_address(raw_addr)
        except ValueError:
            return False
        return any(addr in network for network in _ALLOWED_NETWORKS)
