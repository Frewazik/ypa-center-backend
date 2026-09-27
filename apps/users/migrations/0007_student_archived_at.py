from django.db import migrations, models

# ПОЧЕМУ lock_timeout: ADD COLUMN и DROP INDEX ждут ACCESS EXCLUSIVE на
# users_student; за долгой транзакцией в очередь встали бы профиль и чекаут —
# лучше упасть через 5 с и повторить. SET LOCAL живёт до конца транзакции
_LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s';"


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0006_pd_consent"),
    ]

    operations = [
        migrations.RunSQL(_LOCK_TIMEOUT, reverse_sql=migrations.RunSQL.noop),
        # ПОЧЕМУ дёшево: nullable-колонка без default — правка метаданных,
        # таблица не переписывается
        migrations.AddField(
            model_name="student",
            name="archived_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Удалён родителем"
            ),
        ),
        # ПОЧЕМУ новый индекс раньше удаления старого: условие нового —
        # подмножество старого, построение на живых данных не упадёт.
        # Откат вернёт полный индекс и упадёт, если родитель успел удалить и
        # заново добавить того же ребёнка, — это честный отказ, не баг
        migrations.AddConstraint(
            model_name="student",
            constraint=models.UniqueConstraint(
                condition=models.Q(("archived_at__isnull", True)),
                fields=("parent", "full_name", "dob"),
                name="uq_student_active_per_parent_name_dob",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="student",
            name="uq_student_per_parent_name_dob",
        ),
        # Операции отката идут в обратном порядке — таймаут для них ставим здесь
        migrations.RunSQL(migrations.RunSQL.noop, reverse_sql=_LOCK_TIMEOUT),
    ]
