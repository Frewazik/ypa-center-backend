from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps


def mark_existing_parents_unknown(
    apps: StateApps, schema_editor: BaseDatabaseSchemaEditor
) -> None:
    # ПОЧЕМУ: поле входит в правило «анкета заполнена». Без заполнения все,
    # кто зарегистрировался раньше, после релиза упёрлись бы в анкету.
    # Источник у них честно неизвестен — UNKNOWN, а не выдуманный вариант
    Parent = apps.get_model("users", "Parent")
    Parent.objects.filter(referral_source="").update(referral_source="UNKNOWN")


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0004_student_unique_per_parent"),
    ]

    operations = [
        # ПОЧЕМУ: константный default в PostgreSQL 11+ не переписывает таблицу
        # и не держит долгую блокировку — безопасно на живой базе
        migrations.AddField(
            model_name="parent",
            name="referral_source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("FRIENDS", "Друзья, знакомые"),
                    ("SOCIAL", "Соцсети (VK, Telegram)"),
                    ("MAPS", "Яндекс.Карты, 2ГИС"),
                    ("SEARCH", "Поиск в интернете"),
                    ("SIGN", "Вывеска, проходил мимо"),
                    ("SCHOOL", "Школа, детский сад"),
                    ("OTHER", "Другое"),
                    ("UNKNOWN", "Не указано"),
                ],
                default="",
                max_length=32,
                verbose_name="Откуда узнали",
            ),
        ),
        # Откат: обратный шаг пустой — следом откатывается AddField и удаляет
        # колонку вместе с проставленными значениями
        migrations.RunPython(
            mark_existing_parents_unknown,
            migrations.RunPython.noop,
        ),
    ]
