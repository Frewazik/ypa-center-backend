from __future__ import annotations

from apps.core.throttling import ClientIPRateThrottle


class EventRegistrationIPThrottle(ClientIPRateThrottle):
    scope = "events_registration"
