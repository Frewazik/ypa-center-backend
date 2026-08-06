from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0006_trial_enrollments"),
    ]

    operations = [
        # ПОЧЕМУ partial: пробных на порядки меньше регулярных записей, а
        # запросы занятости всегда ходят парой (schedule_id, trial_date) и
        # только по живым броням. Узкий индекс снимает основную работу с JOIN
        # в недельной сетке и с GROUP BY в чекауте.
        # ПОЧЕМУ порядок условий: Q(**kwargs) сортирует ключи по алфавиту
        migrations.AddIndex(
            model_name="enrollment",
            index=models.Index(
                condition=models.Q(
                    ("status__in", ("HELD", "ENROLLED")),
                    ("type", "TRIAL"),
                ),
                fields=["schedule", "trial_date"],
                name="ix_enroll_trial_slot_date",
            ),
        ),
    ]
