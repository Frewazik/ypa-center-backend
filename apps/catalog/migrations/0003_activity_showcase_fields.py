from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0002_activity_category_activity_price"),
    ]

    operations = [
        migrations.AddField(
            model_name="activity",
            name="cover_image",
            field=models.URLField(
                blank=True, max_length=500, verbose_name="Обложка (URL)"
            ),
        ),
        migrations.AddField(
            model_name="activity",
            name="short_description",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Краткое описание"
            ),
        ),
        migrations.AddField(
            model_name="activity",
            name="features",
            field=models.JSONField(
                blank=True, default=list, verbose_name="Особенности"
            ),
        ),
        migrations.AddField(
            model_name="activity",
            name="tags",
            field=models.JSONField(blank=True, default=list, verbose_name="Теги"),
        ),
    ]
