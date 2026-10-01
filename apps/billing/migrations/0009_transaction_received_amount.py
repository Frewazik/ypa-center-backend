from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0008_enrollment_unique_regular_only"),
    ]

    operations = [
        # ПОЧЕМУ: ADD COLUMN ... NULL без DEFAULT — правка только каталога,
        # таблица не переписывается
        migrations.AddField(
            model_name="transaction",
            name="received_amount",
            field=models.IntegerField(
                blank=True,
                null=True,
                verbose_name="Получено от провайдера, в копейках",
            ),
        ),
    ]
