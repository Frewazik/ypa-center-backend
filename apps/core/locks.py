from __future__ import annotations

import hashlib

from django.db import connection


def text_lock_key(value: str) -> int:
    # ПОЧЕМУ: pg_advisory_lock принимает signed bigint; сдвиг вправо
    # гасит старший бит, иначе половина хэшей переполнит int8
    return int(hashlib.sha256(value.encode()).hexdigest()[:16], 16) >> 1


def try_advisory_xact_lock(key: int) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [key])
        row = cursor.fetchone()
        # SELECT функции всегда возвращает ровно одну строку; assert сужает
        # Optional для mypy strict, обрыв соединения даёт исключение драйвера
        assert row is not None
        return bool(row[0])


def advisory_xact_lock(namespace: int, key: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [namespace, key])
