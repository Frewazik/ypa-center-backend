from __future__ import annotations

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import F


def fill_amount_from_current_price(
    apps: Apps, schema_editor: BaseDatabaseSchemaEditor
) -> None:
    # ПОЧЕМУ по текущей цене: цены на момент старых записей нигде не сохранились —
    # это лучшее приближение, новые брони пишут снимок сами
    EventRegistration = apps.get_model("events", "EventRegistration")
    Event = apps.get_model("events", "Event")
    for event_id, price in Event.objects.values_list("pk", "price").iterator():
        EventRegistration.objects.filter(event_id=event_id, amount__isnull=True).update(
            amount=F("attendees_count") * price
        )


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0003_registration_active_phone_unique"),
    ]

    operations = [
        migrations.AddField(
            model_name="eventregistration",
            name="amount",
            field=models.IntegerField(
                null=True, verbose_name="Сумма брони, в копейках"
            ),
        ),
        migrations.RunPython(fill_amount_from_current_price, migrations.RunPython.noop),
    ]
