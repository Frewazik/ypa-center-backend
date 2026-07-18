from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0002_teacherprofile"),
    ]

    operations = [
        migrations.AddField(
            model_name="teacherprofile",
            name="photo_url",
            field=models.URLField(
                blank=True, max_length=500, verbose_name="Фото (URL)"
            ),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="position",
            field=models.CharField(
                blank=True, max_length=150, verbose_name="Должность на витрине"
            ),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="quote",
            field=models.CharField(blank=True, max_length=255, verbose_name="Цитата"),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="bio",
            field=models.TextField(blank=True, verbose_name="О преподавателе"),
        ),
    ]
