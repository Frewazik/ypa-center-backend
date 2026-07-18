from __future__ import annotations

from apps.core.throttling import ClientIPRateThrottle


class EventRegistrationIPThrottle(ClientIPRateThrottle):
    rate = "3/min"
    scope = "events_registration"
