from __future__ import annotations

from rest_framework.request import Request
from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle
from rest_framework.views import APIView

# ---------------------------------------------------------------------------
# Глобальный rate limiting OTP-эндпоинтов (CWE-400, доставка спама).
#
# Cooldown в services.request_otp защищает ОДИН email. Но эндпоинт публичный:
# скрипт с уникальным сгенерированным email на каждый запрос проходит и
# advisory lock (хэши разные), и cooldown (истории нет) — 1 000 rps молча
# превращаются в 1 000 строк в БД и 1 000 задач в брокере, а почтовый
# провайдер банит домен за спам. Барьер ставится в слое представления,
# ДО сервиса: мусор не должен доходить ни до БД, ни до Taskiq.
#
# Два независимых измерения — как в public-forms-design.md §2.1: нельзя
# ограничивать только по IP (боты ходят с пулов адресов) и только по email
# (один IP может долбить разными ящиками).
#
# Цифры лимитов — стартовые значения из api-core-contracts.md (Сценарий 1:
# «не более 5 запросов кода / час на email и на IP»), подбираются по
# реальному трафику.
#
# ВАЖНО (прод): DRF-троттлинг хранит счётчики в default-кэше. В настройках
# default cache обязан быть Redis — LocMemCache не разделяется между
# воркерами, и лимит «дырявится» пропорционально их числу
# (public-forms-design.md §2.1).
# ---------------------------------------------------------------------------


class OTPRequestPerIPThrottle(AnonRateThrottle):
    """Запрос кода: не более 5/час с одного IP."""

    scope = "otp_request_ip"
    rate = "5/hour"


class OTPRequestPerEmailThrottle(SimpleRateThrottle):
    """
    Запрос кода: не более 5/час на один email — независимо от IP.

    Ключ — нормализованный email из тела запроса (та же канонизация, что
    в services._normalize_email: боту нельзя обойти лимит регистром).
    Дополняет 60-секундный cooldown сервисного слоя: cooldown ограничивает
    частоту (1/мин), этот троттл — суммарный объём за час (5, а не 60).
    """

    scope = "otp_request_email"
    rate = "5/hour"

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        email = request.data.get("email")
        if not isinstance(email, str) or not email.strip():
            # Нет email — правило не применяем; формат отсеет сериализатор.
            return None
        return self.cache_format % {
            "scope": self.scope,
            "ident": email.strip().lower(),
        }


class OTPVerifyPerIPThrottle(AnonRateThrottle):
    """
    Верификация кода: не более 10/мин с одного IP.

    Перебор одного кода уже ограничен attempts_count (5 попыток на токен),
    но распределённый спрей по МНОЖЕСТВУ ящиков с одного IP им не покрыт —
    каждый email даёт атакующему свежий лимит попыток. Живому пользователю
    10 вводов в минуту хватает с запасом (стартовая цифра, тюнится).
    """

    scope = "otp_verify_ip"
    rate = "10/min"
