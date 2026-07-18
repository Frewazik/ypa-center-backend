from __future__ import annotations

import pytest
from django.db import connection, transaction

from apps.core.locks import advisory_xact_lock, text_lock_key, try_advisory_xact_lock

_INT8_MAX = 2**63 - 1


class TestTextLockKey:
    def test_deterministic(self) -> None:
        assert text_lock_key("a@example.com") == text_lock_key("a@example.com")

    def test_distinct_inputs_produce_distinct_keys(self) -> None:
        assert text_lock_key("a@example.com") != text_lock_key("b@example.com")

    def test_fits_signed_bigint(self) -> None:
        assert 0 <= text_lock_key("a@example.com") <= _INT8_MAX


@pytest.mark.django_db(transaction=True)
class TestTryAdvisoryXactLock:
    def test_free_lock_acquired(self) -> None:
        with transaction.atomic():
            assert try_advisory_xact_lock(text_lock_key("free@example.com")) is True

    def test_reentrant_within_same_transaction(self) -> None:
        # ПОЧЕМУ: фиксируем штатное поведение PostgreSQL — в рамках одной
        # сессии повторный захват того же лока не блокируется
        key = text_lock_key("reentrant@example.com")
        with transaction.atomic():
            assert try_advisory_xact_lock(key) is True
            assert try_advisory_xact_lock(key) is True

    def test_released_after_transaction_end(self) -> None:
        key = text_lock_key("released@example.com")
        with transaction.atomic():
            assert try_advisory_xact_lock(key) is True
        with transaction.atomic():
            assert try_advisory_xact_lock(key) is True


@pytest.mark.django_db(transaction=True)
class TestAdvisoryXactLock:
    def test_lock_visible_in_pg_locks_until_commit(self) -> None:
        with transaction.atomic():
            advisory_xact_lock(101, 202)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE locktype = 'advisory' AND classid = 101 AND objid = 202"
                )
                row = cursor.fetchone()
        assert row is not None
        assert row[0] == 1

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND classid = 101 AND objid = 202"
            )
            row = cursor.fetchone()
        assert row is not None
        assert row[0] == 0
