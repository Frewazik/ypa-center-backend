from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    # ПОЧЕМУ отдельная миграция: ALTER после UPDATE тех же строк в одной
    # транзакции PostgreSQL может отказать («pending trigger events»)
    dependencies = [
        ("events", "0004_eventregistration_amount"),
    ]

    operations = [
        migrations.AlterField(
            model_name="eventregistration",
            name="amount",
            field=models.IntegerField(verbose_name="Сумма брони, в копейках"),
        ),
        migrations.AddConstraint(
            model_name="eventregistration",
            constraint=models.CheckConstraint(
                condition=models.Q(("amount__gte", 0)),
                name="event_registration_amount_non_negative",
            ),
        ),
    ]
