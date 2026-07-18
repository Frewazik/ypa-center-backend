from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies: list[tuple[str, str]] = []

    operations = [
        migrations.CreateModel(
            name="GalleryImage",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "image_url",
                    models.URLField(max_length=500, verbose_name="Изображение (URL)"),
                ),
                (
                    "order",
                    models.IntegerField(
                        db_index=True, default=0, verbose_name="Порядок"
                    ),
                ),
                (
                    "is_published",
                    models.BooleanField(default=False, verbose_name="Опубликовано"),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Создано"),
                ),
            ],
            options={
                "db_table": "gallery_image",
                "verbose_name": "Фото галереи",
                "verbose_name_plural": "Фото галереи",
                "ordering": ("order", "id"),
            },
        ),
    ]
