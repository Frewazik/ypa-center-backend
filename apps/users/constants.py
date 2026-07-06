from __future__ import annotations

from typing import Final

OTP_LENGTH: Final[int] = 6
OTP_TTL_MINUTES: Final[int] = 5
OTP_CODE_TTL_SECONDS: Final[int] = OTP_TTL_MINUTES * 60
OTP_MAX_ATTEMPTS: Final[int] = 5
OTP_COOLDOWN_SECONDS: Final[int] = 60

# ПОЧЕМУ: 30 дней — окно для разбора инцидентов безопасности (попытки брутфорса).
OTP_USED_RETENTION_DAYS: Final[int] = 30
