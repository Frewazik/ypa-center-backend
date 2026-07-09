"""Включение расширения PostgreSQL ``btree_gist``.

Обязано примениться раньше начальной миграции моделей: оба
``ExclusionConstraint`` используют GiST-операторы равенства по скалярным
колонкам (``teacher_id``, ``room_id``, ``day_of_week``), которые даёт именно
``btree_gist``.

Начальная миграция моделей генерируется штатно:

    uv run python manage.py makemigrations schedule

Она автоматически встанет в цепочку после этой (``0002_initial``). Пользователю
БД нужны права на ``CREATE EXTENSION`` (либо расширение предустанавливается
DBA/в образе).
"""

from django.contrib.postgres.operations import BtreeGistExtension
from django.db import migrations


class Migration(migrations.Migration):
    dependencies: list[tuple[str, str]] = []

    operations = [
        BtreeGistExtension(),
    ]
