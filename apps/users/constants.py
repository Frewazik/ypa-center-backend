from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Доменные константы OTP-аутентификации.
#
# Вынесены в отдельный модуль (а не в services.py) по двум причинам:
# 1. tasks.py не может импортировать их из services.py — services.py сам
#    импортирует tasks.py (send_otp_email_task), возник бы циклический импорт.
# 2. views.py и тесты импортировали приватные имена (_OTP_COOLDOWN_SECONDS)
#    через границу модуля — приватность, которая нарушается на первом же
#    потребителе, ложная. Здесь имена публичны по замыслу.
# ---------------------------------------------------------------------------

OTP_LENGTH: Final[int] = 6
OTP_TTL_MINUTES: Final[int] = 5
OTP_CODE_TTL_SECONDS: Final[int] = OTP_TTL_MINUTES * 60
OTP_MAX_ATTEMPTS: Final[int] = 5
OTP_COOLDOWN_SECONDS: Final[int] = 60

# Сколько дней хранить использованные (is_used=True) токены до удаления
# фоновой очисткой — окно для разбора инцидентов по attempts_count/датам.
OTP_USED_RETENTION_DAYS: Final[int] = 30
