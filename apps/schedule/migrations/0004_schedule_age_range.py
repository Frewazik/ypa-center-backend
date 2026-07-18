from django.db import migrations, models
from django.db.models import F, Q


class Migration(migrations.Migration):
    dependencies = [
        ("schedule", "0003_schedule_sync_triggers"),
    ]

    operations = [
        migrations.AddField(
            model_name="schedule",
            name="age_min",
            field=models.PositiveSmallIntegerField(
                blank=True, null=True, verbose_name="Возраст от"
            ),
        ),
        migrations.AddField(
            model_name="schedule",
            name="age_max",
            field=models.PositiveSmallIntegerField(
                blank=True, null=True, verbose_name="Возраст до"
            ),
        ),
        migrations.AddConstraint(
            model_name="schedule",
            constraint=models.CheckConstraint(
                condition=Q(("age_min__isnull", True))
                | Q(("age_max__isnull", True))
                | Q(("age_max__gte", F("age_min"))),
                name="schedule_age_range_valid",
            ),
        ),
    ]
