from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0003_attendance_comment"),
    ]

    operations = [
        migrations.AddField(
            model_name="subscriptionplan",
            name="is_unlimited",
            field=models.BooleanField(default=False, verbose_name="Безлимит"),
        ),
        migrations.AddField(
            model_name="subscriptionplan",
            name="is_active",
            field=models.BooleanField(default=True, verbose_name="Активен"),
        ),
    ]
