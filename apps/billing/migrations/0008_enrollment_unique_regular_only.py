from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0007_enrollment_trial_slot_date_index"),
    ]

    operations = [
        # ПОЧЕМУ lock_timeout: DROP INDEX ждёт ACCESS EXCLUSIVE, за долгой
        # транзакцией в очередь встали бы все чекауты — лучше упасть и повторить.
        # SET LOCAL живёт до конца транзакции миграции
        migrations.RunSQL(
            "SET LOCAL lock_timeout = '5s';",
            reverse_sql=migrations.RunSQL.noop,
        ),
        # ПОЧЕМУ новый индекс раньше удаления старого: ACCESS EXCLUSIVE от DROP
        # держится до коммита, поэтому DROP последний. Условие нового —
        # подмножество старого, построение на живых данных не упадёт.
        # ПОЧЕМУ порядок условий: Q(**kwargs) сортирует ключи по алфавиту
        migrations.AddConstraint(
            model_name="enrollment",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("status__in", ("HELD", "ENROLLED")),
                    ("type", "REGULAR"),
                ),
                fields=("student", "schedule"),
                name="uq_billing_active_regular_per_student_slot",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="enrollment",
            name="uq_billing_active_enrollment_per_student_slot",
        ),
    ]
