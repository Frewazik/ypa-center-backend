from __future__ import annotations

from typing import Any

import pytest
from django.core.checks import run_checks


@pytest.mark.django_db
def test_system_checks_do_not_query_database(
    django_assert_num_queries: Any,
) -> None:
    # ПОЧЕМУ ноль запросов: проверки идут перед любой командой manage.py,
    # включая migrate на пустой базе, где таблиц ещё нет (unfold ходил в
    # auth_permission за правом «app.codename» в @action)
    with django_assert_num_queries(0):
        errors = run_checks()

    assert errors == []
