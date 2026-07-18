from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0003_activity_showcase_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="activity",
            name="description",
            field=models.TextField(blank=True, verbose_name="Описание"),
        ),
    ]
