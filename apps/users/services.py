from __future__ import annotations

import secrets
import string
from dataclasses import dataclass
from datetime import timedelta

from asgiref.sync import async_to_sync
from django.db import models, transaction
from django.utils import timezone

from apps.core.locks import text_lock_key, try_advisory_xact_lock
from apps.users.constants import (
    OTP_COOLDOWN_SECONDS,
    OTP_LENGTH,
    OTP_MAX_ATTEMPTS,
    OTP_TTL_MINUTES,
    OTP_USED_RETENTION_DAYS,
)
from apps.users.models import MagicTokens, Parent
from apps.users.tasks import send_otp_email_task
from rest_framework_simplejwt.tokens import RefreshToken


class OTPNotFoundError(Exception):
    pass


class OTPExpiredError(Exception):
    pass


class OTPInvalidError(Exception):
    pass


class OTPBruteForceError(Exception):
    pass


class OTPCooldownError(Exception):
    def __init__(
        self,
        message: str = "",
        retry_after: int = OTP_COOLDOWN_SECONDS,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class TokenPair:
    access: str
    refresh: str


@dataclass(frozen=True, slots=True)
class PurgeResult:
    expired_unused: int
    retired_used: int


def _normalize_email(email: str) -> str:
    # ПОЧЕМУ: канонизация email обязательна для вычисления
    # детерминированного хэша в advisory_lock и уникальности Parent
    return email.strip().lower()


def _generate_code() -> str:
    return "".join(secrets.choice(string.digits) for _ in range(OTP_LENGTH))


def _acquire_email_lock(email: str) -> bool:
    # ПОЧЕМУ: для нового адреса нет строки под SELECT FOR UPDATE —
    # гонку первого запроса закрывает advisory lock по хэшу email
    return try_advisory_xact_lock(text_lock_key(email))


def request_otp(email: str) -> None:
    email = _normalize_email(email)
    now = timezone.now()
    cooldown_threshold = now - timedelta(seconds=OTP_COOLDOWN_SECONDS)

    with transaction.atomic():
        if not _acquire_email_lock(email):
            raise OTPCooldownError(
                "Параллельный запрос кода уже обрабатывается для этого email",
                retry_after=OTP_COOLDOWN_SECONDS,
            )

        last_token = (
            MagicTokens.objects.filter(email=email)
            .order_by("-created_at")
            .values("created_at")
            .first()
        )

        if last_token is not None and last_token["created_at"] > cooldown_threshold:
            elapsed = (now - last_token["created_at"]).total_seconds()
            remaining = max(1, OTP_COOLDOWN_SECONDS - int(elapsed))
            raise OTPCooldownError(
                f"Повторный запрос доступен через {remaining} с",
                retry_after=remaining,
            )

        MagicTokens.objects.filter(email=email, is_used=False).update(is_used=True)

        code = _generate_code()
        expires_at = now + timedelta(minutes=OTP_TTL_MINUTES)

        MagicTokens.objects.create(
            email=email,
            code=code,
            expires_at=expires_at,
        )

        # ПОЧЕМУ: task.kiq() возвращает корутину.
        # Без async_to_sync она молча сбросится
        # внутри синхронного хука on_commit, и письмо не уйдет
        transaction.on_commit(
            lambda: async_to_sync(send_otp_email_task.kiq)(email, code)
        )


def verify_otp(email: str, code: str) -> TokenPair:
    email = _normalize_email(email)
    error_to_raise: Exception | None = None
    parent: Parent | None = None

    with transaction.atomic():
        token = (
            MagicTokens.objects.select_for_update(of=("self",))
            .filter(email=email, is_used=False)
            # ПОЧЕМУ: тай-брейкер по id гарантирует
            # детерминированную выборку при коллизии created_at
            .order_by("-created_at", "-id")
            .first()
        )

        if token is None:
            error_to_raise = OTPNotFoundError("Токен не найден или уже использован")
        elif token.expires_at < timezone.now():
            error_to_raise = OTPExpiredError("Срок действия кода истёк")
        elif token.attempts_count >= OTP_MAX_ATTEMPTS:
            error_to_raise = OTPBruteForceError("Превышен лимит попыток ввода кода")
        # ПОЧЕМУ: защита от тайминг-атак при побайтовом сравнении строк
        elif not secrets.compare_digest(token.code, code):
            MagicTokens.objects.filter(pk=token.pk).update(
                attempts_count=models.F("attempts_count") + 1
            )
            error_to_raise = OTPInvalidError("Неверный код")
        else:
            token.is_used = True
            token.save(update_fields=["is_used"])
            # ПОЧЕМУ: сжигаем возможные орфанные токены от
            # параллельных запросов, избежавших cooldown-блокировки
            MagicTokens.objects.filter(email=email, is_used=False).update(is_used=True)
            try:
                parent = Parent.objects.get(email=email)
            except Parent.DoesNotExist:
                parent = Parent.objects.create_user(email=email, full_name="")

            # ПОЧЕМУ: маскируем деактивированный аккаунт под неверный код,
            # чтобы не раскрывать статус
            if not parent.is_active:
                error_to_raise = OTPInvalidError("Аккаунт деактивирован")
                parent = None

    if error_to_raise is not None:
        raise error_to_raise

    if parent is None:
        raise OTPNotFoundError("Не удалось связать профиль пользователя.")

    refresh: RefreshToken = RefreshToken.for_user(parent)
    return TokenPair(
        access=str(refresh.access_token),
        refresh=str(refresh),
    )


def purge_stale_otp_tokens() -> PurgeResult:
    now = timezone.now()

    expired_unused, _ = MagicTokens.objects.filter(
        is_used=False,
        expires_at__lt=now,
    ).delete()

    retention_threshold = now - timedelta(days=OTP_USED_RETENTION_DAYS)
    retired_used, _ = MagicTokens.objects.filter(
        is_used=True,
        expires_at__lt=retention_threshold,
    ).delete()

    return PurgeResult(expired_unused=expired_unused, retired_used=retired_used)
