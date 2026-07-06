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
    # ПОЧЕМУ: протокол для Mypy strict,
    # чтобы не тянуть внутренние типы pytest_django

    def __call__(
        self, *, using: str = ..., execute: bool = ...
    ) -> AbstractContextManager[list[Callable[[], None]]]: ...


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
    # ПОЧЕМУ: auto_now_add при INSERT игнорирует фабрику;
    # обходим через прямой QuerySet.update()
    MagicTokens.objects.filter(email=email).update(
        created_at=timezone.now() - timedelta(seconds=seconds_ago)
    )


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture(autouse=True)
def _clear_default_cache() -> None:
    from django.core.cache import cache

    cache.clear()


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
        # ПОЧЕМУ: инвариант unusable-пароля теперь
        # жестко обеспечивается менеджером, а не сигналами
        parent = Parent.objects.create_user(
            email="createuser@example.com",
            full_name="",
        )
        assert not parent.has_usable_password()

    def test_create_superuser_with_password_unlocks_admin(self) -> None:
        # ПОЧЕМУ: Django Admin требует check_password();
        # суперпользователю критичен usable-пароль
        admin = Parent.objects.create_superuser(
            email="admin@example.com",
            password="S3cret!pass",
        )
        assert admin.is_staff is True
        assert admin.is_superuser is True
        assert admin.has_usable_password()
        assert admin.check_password("S3cret!pass")

    def test_create_superuser_without_password_stays_unusable(self) -> None:
        # ПОЧЕМУ: вызов --noinput передает password=None,
        #  пароль должен остаться unusable
        admin = Parent.objects.create_superuser(email="noinput@example.com")
        assert admin.is_staff is True
        assert not admin.has_usable_password()

    def test_create_user_does_not_accept_password(self) -> None:
        sig = inspect.signature(Parent.objects.create_user)
        assert "password" not in sig.parameters

    def test_create_user_normalizes_email_to_lowercase(self) -> None:
        # ПОЧЕМУ: защита от дублирования аккаунтов
        # при создании юзера через админку или менеджмент-команды
        parent = Parent.objects.create_user(email="UPPER@Example.COM")
        assert parent.email == "upper@example.com"


@pytest.mark.django_db
class TestStudentModel:
    def test_str_returns_full_name(self) -> None:
        # __str__ возвращает только full_name — без обращения к self.parent.email,
        # чтобы не инициировать ленивый SQL-запрос (N+1 в Admin UI / логах)
        student = StudentFactory(full_name="Иванов Иван Иванович")
        assert str(student) == "Иванов Иван Иванович"

    def test_str_does_not_query_parent(self) -> None:
        # ПОЧЕМУ: регрессионный тест на N+1;
        # обращение к __str__ модели не должно триггерить ленивый SQL
        from django.db import connection as _conn
        from django.test.utils import CaptureQueriesContext

        student = StudentFactory()
        # Детач объекта от кэша — принудительный сброс __dict__ кэша Django ORM
        student.refresh_from_db()
        # Обнуляем кэш related объекта, чтобы parent не был подгружен заранее
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
        indexes = {idx.name: idx.fields for idx in MagicTokens._meta.indexes}
        assert indexes.get("mt_used_expires_idx") == ["is_used", "expires_at"]
        assert indexes.get("mt_email_created_idx") == ["email", "-created_at"]
        assert all(len(name) <= 30 for name in indexes)

    def test_no_index_leads_with_email_before_status_columns(self) -> None:
        for idx in MagicTokens._meta.indexes:
            assert idx.fields != ["email", "is_used", "expires_at"]
            assert idx.fields != ["email", "is_used", "-created_at"]


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
        # ПОЧЕМУ: канонизация email выполняется
        # до БД для стабильного вычисления lock_id
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
        # ПОЧЕМУ: проверка использования криптографически
        # стойкого CSPRNG (secrets) вместо random
        source = inspect.getsource(_generate_code)
        assert "secrets.choice" in source
        assert "random" not in source

    def test_task_is_sync_not_async(self) -> None:
        # ПОЧЕМУ: сетевой SMTP I/O внутри async def
        # заблокирует event loop воркера. Таск обязан быть синхронным
        from apps.users.tasks import send_otp_email_task as task_fn

        # Taskiq оборачивает функцию в объект Task; добираемся до оригинала.
        original = getattr(task_fn, "original_func", task_fn)
        assert not inspect.iscoroutinefunction(original), ()

    def test_on_commit_fires_task(
        self, django_capture_on_commit_callbacks: CaptureOnCommitCallbacks
    ) -> None:
        # ПОЧЕМУ: хуки on_commit выполняются только при
        # фиксации транзакции; проверяем реальный await корутины Taskiq
        with patch("apps.users.services.send_otp_email_task") as mock_task:
            mock_task.kiq = AsyncMock()
            with django_capture_on_commit_callbacks(execute=True) as callbacks:
                request_otp("task@example.com")

        assert len(callbacks) == 1
        # ПОЧЕМУ: kiq — корутина. Проверяем именно await,
        # иначе синхронный on_commit молча ее бросит
        mock_task.kiq.assert_awaited_once()
        email_arg = mock_task.kiq.await_args.args[0]
        assert email_arg == "task@example.com"

    def test_raises_cooldown_within_interval(self) -> None:
        with patch("apps.users.services.send_otp_email_task") as mock_task:
            mock_task.kiq = AsyncMock()
            request_otp("cooldown@example.com")

        with pytest.raises(OTPCooldownError):
            request_otp("cooldown@example.com")

    def test_cooldown_error_carries_actual_remaining_seconds(self) -> None:
        # ПОЧЕМУ: бэкенд авторитетен по времени,
        # отдаем клиенту фактический остаток cooldown, а не константу
        with patch("apps.users.services.send_otp_email_task") as mock_task:
            mock_task.kiq = AsyncMock()
            request_otp("remain@example.com")
        _backdate_token("remain@example.com", seconds_ago=45)

        with pytest.raises(OTPCooldownError) as exc_info:
            request_otp("remain@example.com")

        # Допуск на время исполнения теста: 1..16 секунд.
        assert 1 <= exc_info.value.retry_after <= OTP_COOLDOWN_SECONDS - 44

    def test_allows_request_after_cooldown_expired(self) -> None:
        old_token = MagicTokensFactory(email="aftercooldown@example.com")
        _backdate_token(
            "aftercooldown@example.com", seconds_ago=OTP_COOLDOWN_SECONDS + 1
        )

        with patch("apps.users.services.send_otp_email_task") as mock_task:
            mock_task.kiq = AsyncMock()
            request_otp("aftercooldown@example.com")

        old_token.refresh_from_db()
        assert old_token.is_used is True


@pytest.mark.django_db(transaction=True)
class TestAcquireEmailLock:
    def test_lock_acquired_returns_true(self) -> None:
        with transaction.atomic():
            assert _acquire_email_lock("lock_true@example.com") is True

    def test_same_email_same_transaction_lock_is_reentrant(self) -> None:
        # ПОЧЕМУ: фиксируем штатное поведение PostgreSQL (lock re-entrancy)
        # внутри одной транзакции блокировка саму себя не ждет
        with transaction.atomic():
            first = _acquire_email_lock("reentrant@example.com")
            second = _acquire_email_lock("reentrant@example.com")
        # Оба True — PostgreSQL разрешает повторный захват того же lock
        # в рамках одной сессии (счётчик acquires).
        assert first is True
        assert second is True

    def test_different_emails_produce_different_lock_ids(self) -> None:
        import hashlib as _hashlib

        def lock_id(email: str) -> int:
            return int(_hashlib.sha256(email.encode()).hexdigest()[:16], 16) >> 1

        assert lock_id("a@example.com") != lock_id("b@example.com")

    def test_parallel_request_blocked_by_advisory_lock(self) -> None:
        # ПОЧЕМУ: симулируем занятость advisory lock.
        # Поток B должен упасть в OTPCooldownError без обращения к СУБД
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
        # ПОЧЕМУ: проверяем поиск токена по канонической форме,
        # даже если передан сырой email
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
        # ПОЧЕМУ: забаненный юзер не должен получить JWT.
        # Ошибка маскируется под неверный код (анти-энумерация)
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
        # ПОЧЕМУ: инкремент обязан зафиксироваться в БД
        # до выброса исключения (выход из блока atomic)
        MagicTokensFactory(email="persist@example.com", code="123456")

        for _ in range(3):
            with pytest.raises(OTPInvalidError):
                verify_otp("persist@example.com", "000000")

        token = MagicTokens.objects.get(email="persist@example.com")
        assert token.attempts_count == 3

    def test_compare_digest_used_for_timing_safety(self) -> None:
        # ПОЧЕМУ: прямое сравнение строк (==) уязвимо к тайминг-атакам;
        # проверяем использование secrets
        from apps.users import services as _svc

        source = inspect.getsource(_svc.verify_otp)
        assert "secrets.compare_digest" in source
        # Прямое сравнение строк кода запрещено.
        assert "token.code ==" not in source
        assert "token.code !=" not in source

    def test_parent_created_atomically_with_token_burn(self) -> None:
        MagicTokensFactory(email="atomic@example.com", code="777777")

        verify_otp("atomic@example.com", "777777")

        token = MagicTokens.objects.get(email="atomic@example.com")
        assert token.is_used is True
        parent = Parent.objects.get(email="atomic@example.com")
        assert not parent.has_usable_password()

    def test_orphan_tokens_burned_on_successful_verify(self) -> None:
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
        # ПОЧЕМУ: FOR UPDATE не гарантирует порядок строк
        # при коллизиях времени; тай-брейкер по id обязателен
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


@pytest.mark.django_db
class TestOTPThrottling:
    def test_request_per_ip_limit_blocks_unique_email_flood(
        self, api_client: APIClient
    ) -> None:
        # ПОЧЕМУ: троттл по IP обязан отсечь атаку
        # до вызова сервисного слоя и создания MagicToken
        with patch("apps.users.views.request_otp") as mock_service:
            for i in range(5):
                resp = api_client.post(
                    "/api/v1/auth/otp/request/",
                    {"email": f"unique{i}@example.com"},
                    content_type="application/json",
                )
                assert resp.status_code == 202

            resp = api_client.post(
                "/api/v1/auth/otp/request/",
                {"email": "unique5@example.com"},
                content_type="application/json",
            )

        assert resp.status_code == 429
        assert "Retry-After" in resp
        # Барьер стоит ПЕРЕД сервисом: шестой запрос до request_otp не дошёл
        assert mock_service.call_count == 5

    def test_request_per_email_limit_survives_ip_rotation(
        self, api_client: APIClient
    ) -> None:
        # ПОЧЕМУ: лимит по email отсекает ботов, ротирующих IP-адреса
        with patch("apps.users.views.request_otp"):
            for i in range(5):
                resp = api_client.post(
                    "/api/v1/auth/otp/request/",
                    {"email": "victim@example.com"},
                    content_type="application/json",
                    REMOTE_ADDR=f"10.0.0.{i + 1}",
                )
                assert resp.status_code == 202

            resp = api_client.post(
                "/api/v1/auth/otp/request/",
                {"email": "VICTIM@example.com"},
                content_type="application/json",
                REMOTE_ADDR="10.0.0.100",
            )

        assert resp.status_code == 429

    def test_verify_per_ip_limit_blocks_code_spray(self, api_client: APIClient) -> None:
        # ПОЧЕМУ: отсекаем спрей-атаку (перебор кодов по разным ящикам с одного IP)
        with patch(
            "apps.users.views.verify_otp", side_effect=OTPNotFoundError
        ) as mock_service:
            for i in range(10):
                resp = api_client.post(
                    "/api/v1/auth/otp/verify/",
                    {"email": f"spray{i}@example.com", "code": "000000"},
                    content_type="application/json",
                )
                assert resp.status_code == 401

            resp = api_client.post(
                "/api/v1/auth/otp/verify/",
                {"email": "spray10@example.com", "code": "000000"},
                content_type="application/json",
            )

        assert resp.status_code == 429
        assert mock_service.call_count == 10


@pytest.mark.django_db
class TestPurgeStaleOtpTokens:
    def test_deletes_expired_unused_immediately(self) -> None:
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
        # ПОЧЕМУ: фильтр без is_used игнорировал бы индекс (нарушение left-prefix B-Tree)
        from apps.users import services as _svc

        source = inspect.getsource(_svc.purge_stale_otp_tokens)
        assert source.count("is_used=False") >= 1
        assert source.count("is_used=True") >= 1


@pytest.mark.django_db
class TestOTPRequestView:
    def test_returns_202(self, api_client: APIClient) -> None:
        with patch("apps.users.views.request_otp") as mock_service:
            resp = api_client.post(
                "/api/v1/auth/otp/request/",
                {"email": "view@example.com"},
                content_type="application/json",
            )

        assert resp.status_code == 202
        mock_service.assert_called_once_with("view@example.com")

    def test_returns_429_with_retry_after_on_cooldown(
        self, api_client: APIClient
    ) -> None:
        with patch("apps.users.views.request_otp", side_effect=OTPCooldownError):
            resp = api_client.post(
                "/api/v1/auth/otp/request/",
                {"email": "cd@example.com"},
                content_type="application/json",
            )
        assert resp.status_code == 429
        assert resp["Retry-After"] == str(OTP_COOLDOWN_SECONDS)

        data = resp.json()
        assert "type" in data
        assert "extensions" in data
        assert "request_id" in data["extensions"]

    def test_retry_after_reflects_exception_payload(
        self, api_client: APIClient
    ) -> None:
        exc = OTPCooldownError("осталось 17 с", retry_after=17)
        with patch("apps.users.views.request_otp", side_effect=exc):
            resp = api_client.post(
                "/api/v1/auth/otp/request/",
                {"email": "cd17@example.com"},
                content_type="application/json",
            )
        assert resp.status_code == 429
        assert resp["Retry-After"] == "17"

    def test_returns_400_on_invalid_email(self, api_client: APIClient) -> None:
        resp = api_client.post(
            "/api/v1/auth/otp/request/",
            {"email": "not-an-email"},
            content_type="application/json",
        )
        assert resp.status_code == 400

        # Проверка контракта RFC 7807
        data = resp.json()
        assert data["type"] == "urn:problem-type:validationerror"
        assert data["title"] == "Validation Error"

        # Проверяем, что парсер извлек деталь ошибки конкретного поля
        params = data["extensions"]["invalid_params"]
        assert len(params) == 1
        assert params[0]["name"] == "email"


@pytest.mark.django_db
class TestOTPVerifyView:
    def test_returns_200_with_tokens(self, api_client: APIClient) -> None:
        fake_tokens: TokenPair = {"access": "acc.tok.en", "refresh": "ref.tok.en"}

        with patch("apps.users.views.verify_otp", return_value=fake_tokens):
            resp = api_client.post(
                "/api/v1/auth/otp/verify/",
                {"email": "v@example.com", "code": "123456"},
                content_type="application/json",
            )

        assert resp.status_code == 200
        assert resp.json()["access"] == "acc.tok.en"

    def test_returns_401_on_invalid_code(self, api_client: APIClient) -> None:
        with patch("apps.users.views.verify_otp", side_effect=OTPInvalidError):
            resp = api_client.post(
                "/api/v1/auth/otp/verify/",
                {"email": "v@example.com", "code": "000000"},
                content_type="application/json",
            )
        assert resp.status_code == 401
        assert resp.json()["code"] == "OTP_INVALID"

    def test_expired_token_returns_same_response_as_wrong_code(
        self, api_client: APIClient
    ) -> None:
        # ПОЧЕМУ: одинаковый ответ маскирует статус аккаунта и время запроса кода
        with patch("apps.users.views.verify_otp", side_effect=OTPExpiredError):
            resp_expired = api_client.post(
                "/api/v1/auth/otp/verify/",
                {"email": "v@example.com", "code": "000000"},
                content_type="application/json",
            )
        with patch("apps.users.views.verify_otp", side_effect=OTPInvalidError):
            resp_invalid = api_client.post(
                "/api/v1/auth/otp/verify/",
                {"email": "v@example.com", "code": "000000"},
                content_type="application/json",
            )

        assert resp_expired.status_code == resp_invalid.status_code == 401
        assert resp_expired.json() == resp_invalid.json()

    def test_returns_429_on_brute_force(self, api_client: APIClient) -> None:
        with patch("apps.users.views.verify_otp", side_effect=OTPBruteForceError):
            resp = api_client.post(
                "/api/v1/auth/otp/verify/",
                {"email": "v@example.com", "code": "000000"},
                content_type="application/json",
            )
        assert resp.status_code == 429
        assert resp.json()["code"] == "RATE_LIMITED"

    def test_returns_400_on_non_digit_code(self, api_client: APIClient) -> None:
        resp = api_client.post(
            "/api/v1/auth/otp/verify/",
            {"email": "v@example.com", "code": "abcdef"},
            content_type="application/json",
        )
        assert resp.status_code == 400

        data = resp.json()
        assert data["type"] == "urn:problem-type:validationerror"

        params = data["extensions"]["invalid_params"]
        assert any(p["name"] == "code" for p in params)
