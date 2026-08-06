from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from django.db import connection, transaction

from apps.core.locks import (
    advisory_xact_lock,
    advisory_xact_lock_many,
    text_lock_key,
    try_advisory_xact_lock,
)

if TYPE_CHECKING:
    from pytest_django import DjangoAssertNumQueries

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


@pytest.mark.django_db(transaction=True)
class TestAdvisoryXactLockMany:
    def _held(self, namespace: int) -> list[int]:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT objid FROM pg_locks "
                "WHERE locktype = 'advisory' AND classid = %s ORDER BY objid",
                [namespace],
            )
            return [row[0] for row in cursor.fetchall()]

    def test_locks_whole_set_in_single_statement(
        self,
        django_assert_num_queries: DjangoAssertNumQueries,
    ) -> None:
        # !!!: один statement на весь набор — иначе горячий контур чекаута
        # держал бы блокировки на N сетевых round-trip'ах
        with transaction.atomic():
            with django_assert_num_queries(1):
                advisory_xact_lock_many(303, [11, 22, 33])
            assert self._held(303) == [11, 22, 33]

        assert self._held(303) == []

    def test_empty_set_touches_no_db(
        self,
        django_assert_num_queries: DjangoAssertNumQueries,
    ) -> None:
        with django_assert_num_queries(0):
            advisory_xact_lock_many(304, [])

    def test_matches_per_key_locking(self) -> None:
        # Пакетный захват обязан быть неотличим от поштучного
        with transaction.atomic():
            advisory_xact_lock_many(305, [7, 8])
            advisory_xact_lock(305, 9)
            assert self._held(305) == [7, 8, 9]
