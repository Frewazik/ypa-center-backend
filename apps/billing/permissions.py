from __future__ import annotations

from ipaddress import ip_address, ip_network

from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.core.net import client_ip

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
    # !!!: IP берём только через apps.core.net.client_ip. Голый REMOTE_ADDR
    # за прокси — это адрес самого прокси (все вебхуки отклонялись бы),
    # а левые адреса X-Forwarded-For пишет клиент — ими можно выдать себя
    # за ЮКассу. client_ip верит только адресу, который дописал свой прокси

    message = "Источник запроса не входит в список разрешённых."

    def has_permission(self, request: Request, view: APIView) -> bool:
        try:
            addr = ip_address(client_ip(request))
        except ValueError:
            return False
        return any(addr in network for network in _ALLOWED_NETWORKS)
