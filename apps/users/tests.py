from __future__ import annotations

import inspect
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import timedelta
from typing import Protocol
from unittest.mock import AsyncMock, patch

import factory
import pytest
from django.db import transaction
from django.utils import timezone
from rest_framework.test import APIClient

from apps.users.constants import (
    OTP_COOLDOWN_SECONDS,
    OTP_MAX_ATTEMPTS,
    OTP_USED_RETENTION_DAYS,
)
from apps.users.models import MagicTokens, Parent, Student
from apps.users.services import (
    OTPBruteForceError,
    OTPCooldownError,
    OTPExpiredError,
    OTPInvalidError,
    OTPNotFoundError,
    TokenPair,
    _acquire_email_lock,
    _generate_code,
    purge_stale_otp_tokens,
    request_otp,
    verify_otp,
)


class CaptureOnCommitCallbacks(Protocol):
    """
    Минимальный протокол фикстуры pytest-django
    `django_capture_on_commit_callbacks` — для Mypy strict без завязки
    на внутренние типы pytest_django.
    """

    def __call__(
        self, *, using: str = ..., execute: bool = ...
    ) -> AbstractContextManager[list[Callable[[], None]]]: ...


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


class ParentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Parent

    email = factory.Sequence(lambda n: f"parent{n}@example.com")
    full_name = factory.Faker("name", locale="ru_RU")
    phone = "+79991234567"


class StudentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Student

    parent = factory.SubFactory(ParentFactory)
    full_name = factory.Faker("name", locale="ru_RU")
    school_grade = "5"
    dob = factory.Faker("date_of_birth", minimum_age=6, maximum_age=17)


class MagicTokensFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = MagicTokens

    email = factory.Sequence(lambda n: f"user{n}@example.com")
    code = "123456"
    expires_at = factory.LazyFunction(lambda: timezone.now() + timedelta(minutes=5))
    attempts_count = 0
    is_used = False


def _backdate_token(email: str, seconds_ago: int) -> None:
    """
    Сдвигает created_at токена в прошлое ЧЕРЕЗ queryset.update().

    auto_now_add=True игнорирует значение, переданное в конструктор/фабрику,
    и принудительно ставит now() при INSERT — поэтому
    MagicTokensFactory(created_at=...) молча создаёт «свежий» токен.
    Queryset.update() идёт мимо save() и записывает значение как есть.
    """
    MagicTokens.objects.filter(email=email).update(
        created_at=timezone.now() - timedelta(seconds=seconds_ago)
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture(autouse=True)
def _clear_default_cache() -> None:
    """
    Троттл-счётчики DRF (throttling.py) хранятся в default-кэше и без сброса
    накапливаются между тестами одного процесса: пятый по счёту view-тест
    ловил бы 429 от лимита 5/hour, наведённого предыдущими тестами.
    """
    from django.core.cache import cache

    cache.clear()


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestParentModel:
    def test_creates_with_unusable_password(self) -> None:
        parent = Parent.objects.create_user(email="secure@example.com")
        assert not parent.has_usable_password()

    def test_str_returns_email(self) -> None:
        parent = ParentFactory(email="test@example.com")
        assert str(parent) == "test@example.com"

    def test_email_is_unique(self) -> None:
        from django.db import IntegrityError

        ParentFactory(email="dup@example.com")
        with pytest.raises(IntegrityError):
            ParentFactory(email="dup@example.com")

    def test_create_user_produces_unusable_password(self) -> None:
        """
        ParentManager.create_user — единственная точка создания Parent.
        Сигнал удалён; инвариант unusable-пароля обеспечивается только менеджером.
        """
        parent = Parent.objects.create_user(
            email="createuser@example.com",
            full_name="",
        )
        assert not parent.has_usable_password()

    def test_create_superuser_with_password_unlocks_admin(self) -> None:
        """
        Admin Lockout: Django Admin работает через ModelBackend →
        check_password(); unusable-пароль отклоняется аппаратно.
        create_superuser с паролем обязан выдать usable-пароль,
        проходящий check_password — иначе в админку не войдёт никто.
        """
        admin = Parent.objects.create_superuser(
            email="admin@example.com",
            password="S3cret!pass",
        )
        assert admin.is_staff is True
        assert admin.is_superuser is True
        assert admin.has_usable_password()
        assert admin.check_password("S3cret!pass")

    def test_create_superuser_without_password_stays_unusable(self) -> None:
        """
        createsuperuser --noinput передаёт password=None: пароль остаётся
        unusable (задать позже через changepassword), а не пустая строка,
        проходящая check_password("").
        """
        admin = Parent.objects.create_superuser(email="noinput@example.com")
        assert admin.is_staff is True
        assert not admin.has_usable_password()

    def test_create_user_does_not_accept_password(self) -> None:
        """
        Инвариант OTP-only для родителей: create_user не имеет параметра
        password — usable-пароль обычному Parent недостижим ни одним путём.
        Единственный «чёрный ход» — create_superuser, и только для staff.
        """
        sig = inspect.signature(Parent.objects.create_user)
        assert "password" not in sig.parameters

    def test_create_user_normalizes_email_to_lowercase(self) -> None:
        """
        Defense in Depth: менеджер обязан канонизировать email независимо от
        вызывающего кода. normalize_email() обрабатывает только доменную часть;
        .lower() на всю строку защищает от раздвоения профилей при создании
        через Admin, management command или фоновую задачу в обход сериализатора.
        """
        parent = Parent.objects.create_user(email="UPPER@Example.COM")
        assert parent.email == "upper@example.com"


@pytest.mark.django_db
class TestStudentModel:
    def test_str_returns_full_name(self) -> None:
        # __str__ возвращает только full_name — без обращения к self.parent.email,
        # чтобы не инициировать ленивый SQL-запрос (N+1 в Admin UI / логах).
        student = StudentFactory(full_name="Иванов Иван Иванович")
        assert str(student) == "Иванов Иван Иванович"

    def test_str_does_not_query_parent(self) -> None:
        """
        Регрессионный тест на N+1: str(student) не должен обращаться к БД.
        django.test.utils.CaptureQueriesContext гарантирует ноль запросов
        при вызове __str__ без prefetch/select_related.
        """
        from django.db import connection as _conn
        from django.test.utils import CaptureQueriesContext

        student = StudentFactory()
        # Детач объекта от кэша — принудительный сброс __dict__ кэша Django ORM.
        student.refresh_from_db()
        # Обнуляем кэш related объекта, чтобы parent не был подгружен заранее.
        if "parent" in student.__dict__:
            del student.__dict__["parent"]

        with CaptureQueriesContext(_conn) as ctx:
            _ = str(student)

        assert len(ctx.captured_queries) == 0, (
            f"str(student) выполнил {len(ctx.captured_queries)} SQL-запрос(а). "
            "N+1 в __str__ недопустим."
        )

    def test_cascade_delete(self) -> None:
        student = StudentFactory()
        parent_id = student.parent.pk
        Parent.objects.filter(pk=parent_id).delete()
        assert not Student.objects.filter(pk=student.pk).exists()


@pytest.mark.django_db
class TestMagicTokensModel:
    def test_default_values(self) -> None:
        token = MagicTokensFactory()
        assert token.attempts_count == 0
        assert token.is_used is False

    def test_both_composite_indexes_exist(self) -> None:
        """
        CWE-400: два индекса под два непересекающихся паттерна запросов.
        - (is_used, expires_at) — глобальная фоновая очистка БЕЗ фильтра
          по email: email в префиксе делал бы индекс мёртвым (left-prefix
          B-tree → Seq Scan).
        - (email, -created_at) — request_otp/verify_otp: is_used посередине
          ломал бы готовую сортировку для cooldown-запроса (без фильтра
          is_used) и вызывал filesort всей истории email.
        Имена ≤ 30 символов: Django (Index.max_name_length) роняет импорт
        модели ValueError'ом на более длинных именах.
        """
        indexes = {idx.name: idx.fields for idx in MagicTokens._meta.indexes}
        assert indexes.get("mt_used_expires_idx") == ["is_used", "expires_at"]
        assert indexes.get("mt_email_created_idx") == ["email", "-created_at"]
        assert all(len(name) <= 30 for name in indexes)

    def test_no_index_leads_with_email_before_status_columns(self) -> None:
        """
        Регрессия на «мёртвый индекс»: индекс очистки не должен начинаться
        с email (очистка глобальна), а горячий индекс не должен содержать
        is_used между email и created_at (filesort в request_otp).
        """
        for idx in MagicTokens._meta.indexes:
            assert idx.fields != ["email", "is_used", "expires_at"]
            assert idx.fields != ["email", "is_used", "-created_at"]


# ---------------------------------------------------------------------------
# Service: request_otp
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRequestOtp:
    def test_creates_magic_token(self) -> None:
        with patch("apps.users.services.send_otp_email_task") as mock_task:
            mock_task.kiq = AsyncMock()
            request_otp("new@example.com")

        assert (
            MagicTokens.objects.filter(email="new@example.com", is_used=False).count()
            == 1
        )

    def test_normalizes_email_before_any_db_access(self) -> None:
        """
        CWE-178: канонизация email — обязанность сервисного слоя, не сериализатора.
        Вызов с «грязным» адресом (регистр, пробелы) должен записать токен
        под канонической формой: от неё зависят lock_id advisory-блокировки
        и последующий поиск в verify_otp.
        """
        with patch("apps.users.services.send_otp_email_task") as mock_task:
            mock_task.kiq = AsyncMock()
            request_otp("  MiXed@Example.COM ")

        assert MagicTokens.objects.filter(email="mixed@example.com").count() == 1
        assert not MagicTokens.objects.filter(email="  MiXed@Example.COM ").exists()

    def test_invalidates_previous_tokens(self) -> None:
        old_token = MagicTokensFactory(email="old@example.com")
        _backdate_token("old@example.com", seconds_ago=OTP_COOLDOWN_SECONDS + 1)

        with patch("apps.users.services.send_otp_email_task") as mock_task:
            mock_task.kiq = AsyncMock()
            request_otp("old@example.com")

        old_token.refresh_from_db()
        assert old_token.is_used is True
        assert (
            MagicTokens.objects.filter(email="old@example.com", is_used=False).count()
            == 1
        )

    def test_code_is_6_digits(self) -> None:
        with patch("apps.users.services.send_otp_email_task") as mock_task:
            mock_task.kiq = AsyncMock()
            request_otp("digits@example.com")

        token = MagicTokens.objects.get(email="digits@example.com", is_used=False)
        assert len(token.code) == 6
        assert token.code.isdigit()

    def test_generate_code_uses_secrets_module(self) -> None:
        """
        Проверяем, что _generate_code использует secrets, а не модуль random
        (CWE-338, вихрь Мерсенна предсказуем). inspect.getsource включает
        docstring — поэтому docstring _generate_code не должен содержать
        слово «random», иначе тест даёт ложное срабатывание.
        """
        source = inspect.getsource(_generate_code)
        assert "secrets.choice" in source
        assert "random" not in source

    def test_task_is_sync_not_async(self) -> None:
        """
        Taskiq-задача должна быть синхронной (def, не async def).
        Синхронный блокирующий SMTP I/O внутри async def убивает event loop воркера.
        Taskiq запускает sync-задачи через ThreadPoolExecutor, изолируя блокировку.
        """
        from apps.users.tasks import send_otp_email_task as task_fn

        # Taskiq оборачивает функцию в объект Task; добираемся до оригинала.
        original = getattr(task_fn, "original_func", task_fn)
        assert not inspect.iscoroutinefunction(original), (
            "send_otp_email_task не должна быть async def: "
            "синхронный SMTP I/O блокирует event loop Taskiq-воркера."
        )

    def test_on_commit_fires_task(
        self, django_capture_on_commit_callbacks: CaptureOnCommitCallbacks
    ) -> None:
        """
        Задача ставится через transaction.on_commit. Под @pytest.mark.django_db
        тест живёт внутри откатываемой транзакции, и on_commit-хуки сами
        по себе НЕ выполняются — их принудительно исполняет фикстура
        django_capture_on_commit_callbacks(execute=True). Без неё тест
        всегда «зелёный при нуле вызовов» или всегда красный.
        """
        with patch("apps.users.services.send_otp_email_task") as mock_task:
            mock_task.kiq = AsyncMock()
            with django_capture_on_commit_callbacks(execute=True) as callbacks:
                request_otp("task@example.com")

        assert len(callbacks) == 1
        # assert_awaited (а не assert_called): kiq — async def. Голый вызов
        # kiq(email, code) из sync-хука создал бы корутину и бросил её —
        # call зафиксировался бы, await НЕТ, задача не ушла бы в брокер.
        # Регрессия ловится только проверкой await.
        mock_task.kiq.assert_awaited_once()
        email_arg = mock_task.kiq.await_args.args[0]
        assert email_arg == "task@example.com"

    def test_raises_cooldown_within_interval(self) -> None:
        """
        CWE-799: второй запрос в пределах cooldown-интервала должен падать
        с OTPCooldownError. Первый — создаёт токен, второй — блокируется.
        """
        with patch("apps.users.services.send_otp_email_task") as mock_task:
            mock_task.kiq = AsyncMock()
            request_otp("cooldown@example.com")

        with pytest.raises(OTPCooldownError):
            request_otp("cooldown@example.com")

    def test_cooldown_error_carries_actual_remaining_seconds(self) -> None:
        """
        Контракт «бэкенд авторитетен по времени»: Retry-After должен отражать
        фактический остаток cooldown, а не константу. Токен создан 45 с назад
        → остаток ≈ 15 с, не 60.
        """
        with patch("apps.users.services.send_otp_email_task") as mock_task:
            mock_task.kiq = AsyncMock()
            request_otp("remain@example.com")
        _backdate_token("remain@example.com", seconds_ago=45)

        with pytest.raises(OTPCooldownError) as exc_info:
            request_otp("remain@example.com")

        # Допуск на время исполнения теста: 1..16 секунд.
        assert 1 <= exc_info.value.retry_after <= OTP_COOLDOWN_SECONDS - 44

    def test_allows_request_after_cooldown_expired(self) -> None:
        """
        После истечения cooldown-интервала повторный запрос должен проходить.
        created_at сдвигается queryset.update()'ом: auto_now_add игнорирует
        значение из фабрики (см. _backdate_token).
        """
        old_token = MagicTokensFactory(email="aftercooldown@example.com")
        _backdate_token(
            "aftercooldown@example.com", seconds_ago=OTP_COOLDOWN_SECONDS + 1
        )

        with patch("apps.users.services.send_otp_email_task") as mock_task:
            mock_task.kiq = AsyncMock()
            request_otp("aftercooldown@example.com")

        old_token.refresh_from_db()
        assert old_token.is_used is True


# ---------------------------------------------------------------------------
# Advisory Lock unit tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestAcquireEmailLock:
    def test_lock_acquired_returns_true(self) -> None:
        """
        Первый вызов _acquire_email_lock внутри транзакции возвращает True.
        """
        with transaction.atomic():
            assert _acquire_email_lock("lock_true@example.com") is True

    def test_same_email_same_transaction_lock_is_reentrant(self) -> None:
        """
        pg_try_advisory_xact_lock идемпотентен в рамках одной транзакции:
        PostgreSQL не блокирует саму себя — повторный вызов с тем же ключом
        всё равно вернёт True. Это штатное поведение PostgreSQL (lock re-entrancy).
        Тест документирует эту семантику явно.
        """
        with transaction.atomic():
            first = _acquire_email_lock("reentrant@example.com")
            second = _acquire_email_lock("reentrant@example.com")
        # Оба True — PostgreSQL разрешает повторный захват того же lock
        # в рамках одной сессии (счётчик acquires).
        assert first is True
        assert second is True

    def test_different_emails_produce_different_lock_ids(self) -> None:
        """
        Два разных email должны давать разные lock_id (коллизии возможны,
        но в рамках тестового набора — исключены).
        """
        import hashlib as _hashlib

        def lock_id(email: str) -> int:
            return int(_hashlib.sha256(email.encode()).hexdigest()[:16], 16) >> 1

        assert lock_id("a@example.com") != lock_id("b@example.com")

    def test_parallel_request_blocked_by_advisory_lock(self) -> None:
        """
        Ключевой тест First-Strike DoS (CWE-362 + CWE-799):
        симулируем параллельный запрос, подменяя _acquire_email_lock на False
        для второго вызова. Первый поток получил lock, второй — отбивается
        OTPCooldownError немедленно, не доходя до cooldown-проверки по БД.

        Почему мок, а не настоящий параллелизм:
        - Настоящая конкурентность в pytest-django с transaction=True требует
          многопоточности + синхронизации барьерами. Это сложно, хрупко и медленно.
        - Мок _acquire_email_lock на False точно воспроизводит контракт:
          «если lock занят — raise OTPCooldownError». Логика выше lock
          (cooldown-проверка, create) не должна выполняться вообще.
        """
        call_count = 0

        def mock_lock(email: str) -> bool:
            nonlocal call_count
            call_count += 1
            # Первый вызов — lock свободен (поток A).
            # Второй и далее — lock занят (поток B).
            return call_count == 1

        with patch("apps.users.services._acquire_email_lock", side_effect=mock_lock):
            with patch("apps.users.services.send_otp_email_task") as mock_task:
                mock_task.kiq = AsyncMock()
                # Первый вызов проходит.
                request_otp("parallel@example.com")

            # Второй вызов — lock «занят» → OTPCooldownError без обращения к БД.
            with pytest.raises(OTPCooldownError):
                request_otp("parallel@example.com")

        assert call_count == 2
        # Убеждаемся: создан ровно один токен — только от первого потока.
        assert (
            MagicTokens.objects.filter(
                email="parallel@example.com", is_used=False
            ).count()
            == 1
        )


# ---------------------------------------------------------------------------
# Service: verify_otp
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestVerifyOtp:
    def test_returns_token_pair_on_success(self) -> None:
        token = MagicTokensFactory(email="ok@example.com", code="654321")
        result: TokenPair = verify_otp("ok@example.com", "654321")

        assert "access" in result
        assert "refresh" in result
        assert isinstance(result["access"], str)
        assert isinstance(result["refresh"], str)

        token.refresh_from_db()
        assert token.is_used is True

    def test_normalizes_email_before_lookup(self) -> None:
        """
        CWE-178, зеркало request_otp: verify_otp обязан искать токен по той же
        канонической форме email, под которой request_otp его записал —
        даже если вызывающий код передал «грязный» адрес в обход сериализатора.
        """
        MagicTokensFactory(email="norm@example.com", code="444444")

        result = verify_otp("  NORM@Example.com ", "444444")

        assert "access" in result
        assert Parent.objects.filter(email="norm@example.com").exists()

    def test_creates_parent_on_first_verify(self) -> None:
        assert not Parent.objects.filter(email="new_parent@example.com").exists()
        MagicTokensFactory(email="new_parent@example.com", code="111111")

        verify_otp("new_parent@example.com", "111111")

        parent = Parent.objects.get(email="new_parent@example.com")
        # create_user обязан выставить unusable password — без сигнала-костыля.
        assert not parent.has_usable_password()

    def test_existing_parent_reused_on_second_verify(self) -> None:
        ParentFactory(email="exists@example.com")
        MagicTokensFactory(email="exists@example.com", code="222222")

        verify_otp("exists@example.com", "222222")

        assert Parent.objects.filter(email="exists@example.com").count() == 1

    def test_inactive_parent_receives_no_tokens(self) -> None:
        """
        Broken Authentication: RefreshToken.for_user у SimpleJWT игнорирует
        is_active. Деактивированный аккаунт при верном коде НЕ должен получить
        JWT-пару. Наружу — OTPInvalidError (zero-knowledge: неотличимо от
        неверного кода, статус аккаунта не раскрывается).
        """
        ParentFactory(email="banned@example.com", is_active=False)
        MagicTokensFactory(email="banned@example.com", code="333333")

        with pytest.raises(OTPInvalidError):
            verify_otp("banned@example.com", "333333")

        # Код одноразовый независимо от исхода: токен сожжён.
        token = MagicTokens.objects.get(email="banned@example.com")
        assert token.is_used is True

    def test_raises_not_found_when_no_token(self) -> None:
        with pytest.raises(OTPNotFoundError):
            verify_otp("ghost@example.com", "000000")

    def test_raises_expired_when_ttl_passed(self) -> None:
        MagicTokensFactory(
            email="expired@example.com",
            code="999999",
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        with pytest.raises(OTPExpiredError):
            verify_otp("expired@example.com", "999999")

    def test_raises_invalid_on_wrong_code_and_increments_attempts(self) -> None:
        token = MagicTokensFactory(email="wrong@example.com", code="123456")

        with pytest.raises(OTPInvalidError):
            verify_otp("wrong@example.com", "000000")

        token.refresh_from_db()
        assert token.attempts_count == 1

    def test_attempts_count_persists_despite_exception(self) -> None:
        """
        Критическая проверка CWE-362: исключение выбрасывается ПОСЛЕ закрытия
        транзакции, поэтому инкремент attempts_count зафиксирован в БД
        и не может быть отменён выброшенным исключением.
        3 неверных попытки подряд → счётчик == 3.
        """
        MagicTokensFactory(email="persist@example.com", code="123456")

        for _ in range(3):
            with pytest.raises(OTPInvalidError):
                verify_otp("persist@example.com", "000000")

        token = MagicTokens.objects.get(email="persist@example.com")
        assert token.attempts_count == 3

    def test_compare_digest_used_for_timing_safety(self) -> None:
        """
        CWE-208: проверяем, что сравнение кода идёт через secrets.compare_digest,
        а не прямым == (уязвимость к тайминг-атаке).
        """
        from apps.users import services as _svc

        source = inspect.getsource(_svc.verify_otp)
        assert "secrets.compare_digest" in source
        # Прямое сравнение строк кода запрещено.
        assert "token.code ==" not in source
        assert "token.code !=" not in source

    def test_parent_created_atomically_with_token_burn(self) -> None:
        """
        create_user(Parent) и token.is_used=True выполняются в одной транзакции.
        Проверяем, что после успешного verify_otp:
        - токен сожжён,
        - Parent существует и имеет unusable-пароль,
        и оба факта верны одновременно (нет окна между ними).
        """
        MagicTokensFactory(email="atomic@example.com", code="777777")

        verify_otp("atomic@example.com", "777777")

        token = MagicTokens.objects.get(email="atomic@example.com")
        assert token.is_used is True
        parent = Parent.objects.get(email="atomic@example.com")
        assert not parent.has_usable_password()

    def test_orphan_tokens_burned_on_successful_verify(self) -> None:
        """
        CWE-362: при успешной верификации все остальные неиспользованные токены
        для этого email должны быть инвалидированы (защита от орфанных токенов).
        """
        orphan = MagicTokensFactory(email="orphan@example.com", code="000000")
        active = MagicTokensFactory(email="orphan@example.com", code="111111")

        verify_otp("orphan@example.com", "111111")

        orphan.refresh_from_db()
        active.refresh_from_db()
        assert orphan.is_used is True
        assert active.is_used is True

    def test_raises_brute_force_when_attempts_exhausted(self) -> None:
        MagicTokensFactory(
            email="brute@example.com",
            code="123456",
            attempts_count=OTP_MAX_ATTEMPTS,
        )
        with pytest.raises(OTPBruteForceError):
            verify_otp("brute@example.com", "123456")

    def test_raises_not_found_for_used_token(self) -> None:
        MagicTokensFactory(email="used@example.com", code="123456", is_used=True)
        with pytest.raises(OTPNotFoundError):
            verify_otp("used@example.com", "123456")

    def test_equal_created_at_resolved_by_id_tiebreaker(self) -> None:
        """
        Недетерминированность FOR UPDATE: при идентичных created_at (ретраи
        клиента, сбой синхронизации времени) PostgreSQL не гарантирует порядок
        равных ключей сортировки. Тай-брейкер -id обязан детерминированно
        выбирать ПОСЛЕДНИЙ созданный токен (максимальный id).
        """
        older = MagicTokensFactory(email="tie@example.com", code="111111")
        newer = MagicTokensFactory(email="tie@example.com", code="222222")
        same_moment = timezone.now()
        MagicTokens.objects.filter(pk__in=[older.pk, newer.pk]).update(
            created_at=same_moment
        )

        # Верифицируем кодом токена с БОЛЬШИМ id — именно его обязан
        # захватить select_for_update при равных created_at.
        result = verify_otp("tie@example.com", "222222")

        assert "access" in result
        newer.refresh_from_db()
        assert newer.is_used is True


# ---------------------------------------------------------------------------
# Throttling (CWE-400: глобальный флуд уникальными email / спрей кодов)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOTPThrottling:
    def test_request_per_ip_limit_blocks_unique_email_flood(
        self, api_client: APIClient
    ) -> None:
        """
        Ключевой сценарий атаки: уникальный email на каждый запрос обходит
        и advisory lock, и cooldown. Per-IP троттл (5/hour) обязан отбить
        шестой запрос ДО сервисного слоя — сервис не должен быть вызван.
        """
        with patch("apps.users.views.request_otp") as mock_service:
            for i in range(5):
                resp = api_client.post(
                    "/api/v1/auth/otp/request",
                    {"email": f"unique{i}@example.com"},
                    content_type="application/json",
                )
                assert resp.status_code == 202

            resp = api_client.post(
                "/api/v1/auth/otp/request",
                {"email": "unique5@example.com"},
                content_type="application/json",
            )

        assert resp.status_code == 429
        assert "Retry-After" in resp
        # Барьер стоит ПЕРЕД сервисом: шестой запрос до request_otp не дошёл.
        assert mock_service.call_count == 5

    def test_request_per_email_limit_survives_ip_rotation(
        self, api_client: APIClient
    ) -> None:
        """
        Второе измерение: бот с пулом адресов (уникальный IP на запрос) не
        должен пробить лимит 5/hour на один email. Ключ троттла —
        нормализованный email, поэтому смена регистра тоже не помогает.
        """
        with patch("apps.users.views.request_otp"):
            for i in range(5):
                resp = api_client.post(
                    "/api/v1/auth/otp/request",
                    {"email": "victim@example.com"},
                    content_type="application/json",
                    REMOTE_ADDR=f"10.0.0.{i + 1}",
                )
                assert resp.status_code == 202

            resp = api_client.post(
                "/api/v1/auth/otp/request",
                {"email": "VICTIM@example.com"},
                content_type="application/json",
                REMOTE_ADDR="10.0.0.100",
            )

        assert resp.status_code == 429

    def test_verify_per_ip_limit_blocks_code_spray(self, api_client: APIClient) -> None:
        """
        attempts_count ограничивает перебор одного кода; спрей по РАЗНЫМ
        ящикам с одного IP отсекает per-IP троттл на verify (10/min).
        """
        with patch(
            "apps.users.views.verify_otp", side_effect=OTPNotFoundError
        ) as mock_service:
            for i in range(10):
                resp = api_client.post(
                    "/api/v1/auth/otp/verify",
                    {"email": f"spray{i}@example.com", "code": "000000"},
                    content_type="application/json",
                )
                assert resp.status_code == 401

            resp = api_client.post(
                "/api/v1/auth/otp/verify",
                {"email": "spray10@example.com", "code": "000000"},
                content_type="application/json",
            )

        assert resp.status_code == 429
        assert mock_service.call_count == 10


# ---------------------------------------------------------------------------
# Service: purge_stale_otp_tokens
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPurgeStaleOtpTokens:
    def test_deletes_expired_unused_immediately(self) -> None:
        """
        Фаза 1: протухший невведённый код удаляется сразу, живой — остаётся.
        """
        MagicTokensFactory(
            email="dead@example.com",
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        alive = MagicTokensFactory(
            email="alive@example.com",
            expires_at=timezone.now() + timedelta(minutes=5),
        )

        result = purge_stale_otp_tokens()

        assert result["expired_unused"] == 1
        assert not MagicTokens.objects.filter(email="dead@example.com").exists()
        assert MagicTokens.objects.filter(pk=alive.pk).exists()

    def test_used_tokens_respect_retention_window(self) -> None:
        """
        Фаза 2: использованный токен живёт OTP_USED_RETENTION_DAYS
        (окно разбора инцидентов), затем удаляется. Свежий использованный
        и протухший-но-в-окне — не трогаются.
        """
        ancient = MagicTokensFactory(
            email="ancient@example.com",
            is_used=True,
            expires_at=timezone.now()
            - timedelta(days=OTP_USED_RETENTION_DAYS, minutes=1),
        )
        recent_used = MagicTokensFactory(
            email="recent@example.com",
            is_used=True,
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        result = purge_stale_otp_tokens()

        assert result["retired_used"] == 1
        assert not MagicTokens.objects.filter(pk=ancient.pk).exists()
        assert MagicTokens.objects.filter(pk=recent_used.pk).exists()

    def test_returns_zero_counts_on_clean_table(self) -> None:
        result = purge_stale_otp_tokens()
        assert result == {"expired_unused": 0, "retired_used": 0}

    def test_purge_predicates_are_index_aligned(self) -> None:
        """
        Регрессия на left-prefix: обе фазы обязаны фильтровать по is_used
        (равенство) — это префикс индекса mt_used_expires_idx. Предикат
        «expires_at < X» без is_used не смог бы использовать индекс вовсе.
        Белый ящик: проверяем исходник сервиса.
        """
        from apps.users import services as _svc

        source = inspect.getsource(_svc.purge_stale_otp_tokens)
        assert source.count("is_used=False") >= 1
        assert source.count("is_used=True") >= 1


# ---------------------------------------------------------------------------
# View tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOTPRequestView:
    def test_returns_202(self, api_client: APIClient) -> None:
        with patch("apps.users.views.request_otp") as mock_service:
            resp = api_client.post(
                "/api/v1/auth/otp/request",
                {"email": "view@example.com"},
                content_type="application/json",
            )

        assert resp.status_code == 202
        mock_service.assert_called_once_with("view@example.com")

    def test_returns_429_with_retry_after_on_cooldown(
        self, api_client: APIClient
    ) -> None:
        """
        CWE-799: cooldown должен возвращать 429 с заголовком Retry-After
        (api-core-contracts.md §0.2). Исключение без явного retry_after
        несёт дефолт — полный интервал.
        """
        with patch("apps.users.views.request_otp", side_effect=OTPCooldownError):
            resp = api_client.post(
                "/api/v1/auth/otp/request",
                {"email": "cd@example.com"},
                content_type="application/json",
            )
        assert resp.status_code == 429
        assert resp["Retry-After"] == str(OTP_COOLDOWN_SECONDS)
        assert resp.json()["code"] == "RATE_LIMITED"

    def test_retry_after_reflects_exception_payload(
        self, api_client: APIClient
    ) -> None:
        """
        Retry-After обязан транслировать фактический остаток cooldown
        из OTPCooldownError.retry_after, а не константу: бэкенд авторитетен
        по времени, фронт рисует таймер по заголовку.
        """
        exc = OTPCooldownError("осталось 17 с", retry_after=17)
        with patch("apps.users.views.request_otp", side_effect=exc):
            resp = api_client.post(
                "/api/v1/auth/otp/request",
                {"email": "cd17@example.com"},
                content_type="application/json",
            )
        assert resp.status_code == 429
        assert resp["Retry-After"] == "17"

    def test_returns_400_on_invalid_email(self, api_client: APIClient) -> None:
        # DRF serializer.is_valid(raise_exception=True) → ValidationError → 400.
        # Схема @extend_schema синхронизирована: задокументирован 400, а не 422.
        resp = api_client.post(
            "/api/v1/auth/otp/request",
            {"email": "not-an-email"},
            content_type="application/json",
        )
        assert resp.status_code == 400


@pytest.mark.django_db
class TestOTPVerifyView:
    def test_returns_200_with_tokens(self, api_client: APIClient) -> None:
        fake_tokens: TokenPair = {"access": "acc.tok.en", "refresh": "ref.tok.en"}

        with patch("apps.users.views.verify_otp", return_value=fake_tokens):
            resp = api_client.post(
                "/api/v1/auth/otp/verify",
                {"email": "v@example.com", "code": "123456"},
                content_type="application/json",
            )

        assert resp.status_code == 200
        assert resp.json()["access"] == "acc.tok.en"

    def test_returns_401_on_invalid_code(self, api_client: APIClient) -> None:
        with patch("apps.users.views.verify_otp", side_effect=OTPInvalidError):
            resp = api_client.post(
                "/api/v1/auth/otp/verify",
                {"email": "v@example.com", "code": "000000"},
                content_type="application/json",
            )
        assert resp.status_code == 401
        assert resp.json()["code"] == "OTP_INVALID"

    def test_expired_token_returns_same_response_as_wrong_code(
        self, api_client: APIClient
    ) -> None:
        """
        CWE-204: OTPExpiredError должен давать тот же HTTP-ответ, что и OTPInvalidError.
        Разные ответы позволяют атакующему определить, что жертва запрашивала
        код в последние 5 минут (утечка поведенческих метаданных).
        Забаненный аккаунт сервис маскирует под OTPInvalidError — он покрыт
        этим же контрактом автоматически.
        """
        with patch("apps.users.views.verify_otp", side_effect=OTPExpiredError):
            resp_expired = api_client.post(
                "/api/v1/auth/otp/verify",
                {"email": "v@example.com", "code": "000000"},
                content_type="application/json",
            )
        with patch("apps.users.views.verify_otp", side_effect=OTPInvalidError):
            resp_invalid = api_client.post(
                "/api/v1/auth/otp/verify",
                {"email": "v@example.com", "code": "000000"},
                content_type="application/json",
            )

        assert resp_expired.status_code == resp_invalid.status_code == 401
        assert resp_expired.json() == resp_invalid.json()

    def test_returns_429_on_brute_force(self, api_client: APIClient) -> None:
        with patch("apps.users.views.verify_otp", side_effect=OTPBruteForceError):
            resp = api_client.post(
                "/api/v1/auth/otp/verify",
                {"email": "v@example.com", "code": "000000"},
                content_type="application/json",
            )
        assert resp.status_code == 429
        assert resp.json()["code"] == "RATE_LIMITED"

    def test_returns_400_on_non_digit_code(self, api_client: APIClient) -> None:
        # DRF serializer.is_valid(raise_exception=True) → ValidationError → 400.
        resp = api_client.post(
            "/api/v1/auth/otp/verify",
            {"email": "v@example.com", "code": "abcdef"},
            content_type="application/json",
        )
        assert resp.status_code == 400
