from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0004_subscriptionplan_showcase_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="transaction",
            name="compensation_claimed_until",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Возврат зарезервирован до"
            ),
        ),
    ]
