from __future__ import annotations

from apps.core.throttling import ClientIPRateThrottle


class PublicFormIPThrottle(ClientIPRateThrottle):
    rate = "3/min"


class CallbackIPThrottle(PublicFormIPThrottle):
    scope = "public_forms_callback"


class FeedbackIPThrottle(PublicFormIPThrottle):
    scope = "public_forms_feedback"
