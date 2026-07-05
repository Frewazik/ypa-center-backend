from __future__ import annotations

import hashlib
import secrets
import string
from datetime import timedelta
from typing import TypedDict

from asgiref.sync import async_to_sync
from django.db import connection, models, transaction
from django.utils import timezone

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


# ---------------------------------------------------------------------------
# Публичные исключения домена
# ---------------------------------------------------------------------------


class OTPNotFoundError(Exception):
    """Активный токен для данного email не найден."""


class OTPExpiredError(Exception):
    """Токен найден, но истёк срок действия."""


class OTPInvalidError(Exception):
    """Код неверный либо аккаунт деактивирован; attempts_count инкрементирован."""


class OTPBruteForceError(Exception):
    """Превышен лимит попыток ввода — токен заблокирован."""


class OTPCooldownError(Exception):
    """
    Повторный запрос кода раньше истечения cooldown-интервала (CWE-799).

    Несёт фактическое число секунд до разблокировки: контракт
    (api-core-contracts.md §1, «бэкенд авторитетен по времени») требует,
    чтобы клиент получал точный Retry-After, а не константу. Если после
    первого запроса прошло 45 с — осталось 15, а не 60.
    """

    def __init__(
        self,
        message: str = "",
        retry_after: int = OTP_COOLDOWN_SECONDS,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# TypedDict для возвращаемых токенов
# ---------------------------------------------------------------------------


class TokenPair(TypedDict):
    access: str
    refresh: str


class PurgeResult(TypedDict):
    """Счётчики удалённых строк по фазам очистки — для логов/метрик воркера."""

    expired_unused: int
    retired_used: int


# ---------------------------------------------------------------------------
# Приватные вспомогательные функции
# ---------------------------------------------------------------------------


def _normalize_email(email: str) -> str:
    """
    Канонизация email — доменный инвариант, а не UI-косметика (CWE-178).

    От канонической формы зависят: детерминированный lock_id advisory-блокировки,
    поиск токена в verify_otp по ключу, записанному в request_otp, и уникальность
    Parent.email. Нормализация живёт в сервисном слое: сервис — глухой бункер
    и не доверяет вызывающему коду. Management-команда или RPC-ручка, вызвавшая
    request_otp("ADMIN@MYSITE.COM") в обход DRF-сериализатора, получит то же
    поведение, что и HTTP-клиент.
    """
    return email.strip().lower()


def _generate_code() -> str:
    """
    Генерирует криптографически безопасный 6-значный цифровой код.
    Использует secrets.choice (CSPRNG), а не вихрь Мерсенна (CWE-338).
    """
    return "".join(secrets.choice(string.digits) for _ in range(OTP_LENGTH))


def _acquire_email_lock(email: str) -> bool:
    """
    Берёт транзакционный PostgreSQL Advisory Lock по детерминированному хэшу email.

    Возвращает True если блокировка получена, False если уже занята параллельной
    транзакцией. Блокировка (xact_lock) автоматически освобождается при завершении
    транзакции — COMMIT или ROLLBACK. Split-brain с Redis невозможен.

    Зачем это нужно (First-Strike DoS, CWE-362 + CWE-799):
    `SELECT FOR UPDATE` берёт row lock на существующую строку. Если email новый
    и в MagicTokens нет ни одной записи, `.first()` возвращает None — PostgreSQL
    в READ COMMITTED не выдаёт gap lock, и 100 параллельных потоков проходят
    cooldown-проверку одновременно, создавая 100 токенов и отправляя 100 писем.
    Advisory lock блокирует не строку, а абстрактный int64-ключ — он существует
    независимо от наличия данных в таблице.

    Алгоритм ключа:
        SHA-256(email) → первые 8 байт → big-endian int64 → сдвиг вправо на 1 бит.
    Сдвиг переводит uint64 в диапазон int64 (PostgreSQL принимает только signed bigint).
    Коллизия возможна, но крайне маловероятна (2^63 пространство); при коллизии
    два разных email сериализуются — это допустимо (ложная задержка, не ложный отказ).

    Функция ожидает уже нормализованный email (вызывается только из request_otp,
    который канонизирует вход первым действием) и активную транзакцию
    (transaction.atomic()).
    """
    # [:16] берёт первые 16 hex-символов (= 8 байт = int64)
    lock_id = int(hashlib.sha256(email.encode()).hexdigest()[:16], 16) >> 1
    with connection.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(%s)", [lock_id])
        row = cur.fetchone()
        if row is None:
            # pg_try_advisory_xact_lock всегда возвращает строку; None возможен
            # только при аномалии драйвера. Fail-closed: считаем lock не взятым —
            # ложный отказ (429) безопаснее ложного пропуска (гонка создания).
            return False
        return bool(row[0])


# ---------------------------------------------------------------------------
# Публичные сервисные функции
# ---------------------------------------------------------------------------


def request_otp(email: str) -> None:
    """
    Генерирует OTP-код, инвалидирует старые токены для email
    и ставит задачу отправки письма строго после коммита транзакции.

    Первое действие — канонизация email (см. _normalize_email): все ключи ниже
    (lock_id, фильтры по email, создаваемый токен) работают с одной формой адреса
    независимо от того, кто и как вызвал сервис.

    Двухуровневая защита от CWE-799 (Email Bombing) и CWE-362 (Race Condition):

    Уровень 1 — Advisory Lock (параллельность):
        pg_try_advisory_xact_lock сериализует конкурентные запросы для одного email.
        Второй параллельный поток не получает lock и сразу получает OTPCooldownError,
        не достигая ни cooldown-проверки, ни CREATE. Это закрывает First-Strike DoS
        на несуществующий email, где select_for_update возвращал бы None.

    Уровень 2 — Cooldown по БД (частота):
        Временна́я проверка по `last_token.created_at` ограничивает частоту запросов
        от одного email до 1 раза в OTP_COOLDOWN_SECONDS. Последовательные запросы
        с интервалом < 60 с отклоняются с точным retry_after — сколько секунд
        реально осталось до разблокировки.

    Raises:
        OTPCooldownError: lock занят параллельным потоком ИЛИ cooldown не истёк.
            Атрибут retry_after — фактические секунды до следующей попытки.
    """
    email = _normalize_email(email)
    now = timezone.now()
    cooldown_threshold = now - timedelta(seconds=OTP_COOLDOWN_SECONDS)

    with transaction.atomic():
        # Уровень 1: сериализация параллельных запросов.
        # Возвращает False если транзакция с тем же lock_id уже активна.
        # retry_after = полный интервал: конкурент прямо сейчас создаёт токен,
        # значит cooldown для него начнётся «сейчас» — полный интервал корректен.
        if not _acquire_email_lock(email):
            raise OTPCooldownError(
                "Параллельный запрос кода уже обрабатывается для этого email",
                retry_after=OTP_COOLDOWN_SECONDS,
            )

        # Уровень 2: cooldown-проверка по временно́й метке последнего токена.
        # После прохождения advisory lock этот SELECT безопасен — конкурентов нет.
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

        # Инвалидируем все неиспользованные токены для этого email.
        MagicTokens.objects.filter(email=email, is_used=False).update(is_used=True)

        code = _generate_code()
        expires_at = now + timedelta(minutes=OTP_TTL_MINUTES)

        MagicTokens.objects.create(
            email=email,
            code=code,
            expires_at=expires_at,
        )

        # Письмо — строго после коммита: откат транзакции не породит отправку.
        #
        # КРИТИЧНО: task.kiq() у Taskiq — это `async def`. Голый вызов
        # `send_otp_email_task.kiq(email, code)` из синхронного on_commit-хука
        # лишь СОЗДАЁТ корутину и тут же её бросает — задача никогда не уходит
        # в брокер, письма молча не отправляются (RuntimeWarning «coroutine was
        # never awaited» тонет в логах). async_to_sync (asgiref — уже зависимость
        # Django, ничего нового не тянем) прогоняет корутину до конца в event
        # loop и гарантирует реальную постановку задачи.
        transaction.on_commit(
            lambda: async_to_sync(send_otp_email_task.kiq)(email, code)
        )


def verify_otp(email: str, code: str) -> TokenPair:
    """
    Верифицирует OTP-код и возвращает пару JWT-токенов.

    Первое действие — канонизация email: гарантирует поиск токена по тому же
    ключу, под которым он был записан в request_otp, каким бы путём ни был
    вызван сервис (HTTP, management command, RPC).

    Протокол (одна транзакция, одна строчная блокировка):

    1. SELECT FOR UPDATE по (email, is_used=False) — берём row lock до любых проверок.
       of=("self",) явно ограничивает блокировку строками MagicTokens.
       Запрос обслуживает индекс mt_email_created_idx (email, created_at DESC):
       PostgreSQL идёт по уже отсортированному хвосту email и отбрасывает
       is_used=True на лету, без filesort. is_used в середине индекса ломал бы
       готовую сортировку для cooldown-запроса request_otp (там фильтра по
       is_used нет) — см. комментарии в models.MagicTokens.Meta.

    2. Все проверки — внутри atomic(). Результат записывается в error_to_raise,
       исключение НЕ выбрасывается изнутри блока. Это гарантирует, что
       attempts_count += 1 (через F()) зафиксируется в БД до того, как
       транзакция закроется, независимо от исхода проверки.

    3. Успешная верификация: сжигаем целевой токен И все остальные неиспользованные
       токены для этого email (защита от орфанных токенов, CWE-362).

    4. Parent создаётся через create_user (не get_or_create) — внутри того же
       atomic(). Токен сожжён и профиль создан атомарно: нет окна, в котором
       is_used=True, но Parent не существует. create_user — единственная точка
       создания Parent; вызов set_unusable_password() явно через менеджер
       исключает необходимость в post_save-сигнале.

    5. Деактивированный аккаунт (is_active=False) НЕ получает токены:
       RefreshToken.for_user у SimpleJWT игнорирует is_active и выдал бы
       криптографически валидную пару забаненному пользователю (Broken
       Authentication). Наружу уходит тот же OTPInvalidError, что и при
       неверном коде — статус аккаунта не раскрывается (zero-knowledge,
       CWE-204). Токен при этом всё равно сожжён: код одноразовый независимо
       от исхода.

    6. Исключение выбрасывается после выхода из with-блока — транзакция уже
       закрыта и закоммичена, откатить инкремент невозможно.

    7. secrets.compare_digest — защита от CWE-208 (тайминг-атака по микрозадержкам
       побайтового сравнения строк).

    Raises:
        OTPNotFoundError: активный токен не найден или уже использован.
        OTPExpiredError: TTL истёк.
        OTPBruteForceError: >= OTP_MAX_ATTEMPTS неудачных попыток.
        OTPInvalidError: код неверный (attempts_count инкрементирован и закоммичен)
            либо аккаунт деактивирован (наружу неотличимо от неверного кода).
    """
    email = _normalize_email(email)
    error_to_raise: Exception | None = None
    parent: Parent | None = None

    with transaction.atomic():
        token = (
            MagicTokens.objects.select_for_update(of=("self",))
            .filter(email=email, is_used=False)
            # Тай-брейкер по id: при коллизии created_at (ретраи клиента,
            # сбой синхронизации времени) PostgreSQL не гарантирует стабильный
            # порядок равных ключей — два вызова могли бы захватить РАЗНЫЕ
            # токены. id монотонен и уникален → выбор детерминирован всегда.
            # Цена: при нескольких строках с равным created_at возможен
            # микроскопический Sort поверх индекса (email, -created_at) — при
            # LIMIT 1 и инвалидации орфанов это единицы строк.
            .order_by("-created_at", "-id")
            .first()
        )

        if token is None:
            error_to_raise = OTPNotFoundError("Токен не найден или уже использован")
        elif token.expires_at < timezone.now():
            error_to_raise = OTPExpiredError("Срок действия кода истёк")
        elif token.attempts_count >= OTP_MAX_ATTEMPTS:
            error_to_raise = OTPBruteForceError("Превышен лимит попыток ввода кода")
        elif not secrets.compare_digest(token.code, code):
            # Атомарный SQL UPDATE на уровне QuerySet: инкремент выполняет БД
            # (read-modify-write в Python исключён), а int-полю инстанса не
            # присваивается F-выражение — тот приём требовал
            # `type: ignore[assignment]` и прятал техдолг под ковёр.
            # Строка уже удержана НАШИМ ЖЕ select_for_update выше — UPDATE
            # проходит без ожидания, дедлок невозможен. refresh_from_db не
            # нужен: attempts_count дальше в этой ветке не читается.
            MagicTokens.objects.filter(pk=token.pk).update(
                attempts_count=models.F("attempts_count") + 1
            )
            error_to_raise = OTPInvalidError("Неверный код")
        else:
            token.is_used = True
            token.save(update_fields=["is_used"])
            # Сжигаем орфанные токены: параллельный request_otp мог создать
            # второй активный токен до того, как cooldown-блокировка сработала.
            # Инвалидируем всё, кроме только что сожжённого (pk=token.pk исключён
            # через filter — он уже is_used=True после save выше).
            MagicTokens.objects.filter(email=email, is_used=False).update(is_used=True)
            # Явное использование create_user вместо get_or_create:
            # - create_user — единственная точка создания Parent, вызывающая
            #   set_unusable_password() явно через менеджер.
            # - get_or_create обходит менеджер и требовал бы сигнала-костыля
            #   для гарантии unusable-пароля (антипаттерн «spooky action at a distance»).
            try:
                parent = Parent.objects.get(email=email)
            except Parent.DoesNotExist:
                parent = Parent.objects.create_user(email=email, full_name="")

            # Пункт 5 докстринга: забаненный аккаунт не получает JWT.
            # Zero-knowledge: снаружи неотличимо от неверного кода.
            if not parent.is_active:
                error_to_raise = OTPInvalidError("Аккаунт деактивирован")
                parent = None

    # Транзакция закрыта и закоммичена. Инкремент зафиксирован в БД.
    # Только теперь выбрасываем исключение — откат невозможен.
    if error_to_raise is not None:
        raise error_to_raise

    # parent гарантированно не None: мы попали сюда только через успешную ветку
    # с активным аккаунтом.
    assert parent is not None  # для Mypy; в рантайме невозможно

    refresh: RefreshToken = RefreshToken.for_user(parent)
    return TokenPair(
        access=str(refresh.access_token),
        refresh=str(refresh),
    )


def purge_stale_otp_tokens() -> PurgeResult:
    """
    Двухфазная фоновая очистка таблицы OTP-токенов.
    Вызывается по расписанию из tasks.purge_stale_otp_tokens_task.

    Фаза 1 — мёртвые активные токены:
        is_used=False AND expires_at < now().
        Протухший невведённый код бесполезен и удаляется немедленно —
        это и гигиена таблицы, и безопасность (меньше живых кодов в БД).

    Фаза 2 — ретеншн использованных:
        is_used=True AND expires_at < now() - OTP_USED_RETENTION_DAYS.
        Использованные токены хранятся окно ретеншна для разбора инцидентов
        (attempts_count, временны́е метки), затем удаляются.

    Обе фазы НАМЕРЕННО сформулированы как «равенство по is_used + диапазон
    по expires_at» — это точный left-prefix индекса mt_used_expires_idx.
    Единый предикат «expires_at < X» без is_used не смог бы использовать
    этот индекс вовсе (B-tree привязан к левому префиксу), а предикат
    с email в префиксе индекса выродился бы в Seq Scan — очистка глобальна
    и по email не фильтрует.
    """
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
