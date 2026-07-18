from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="activity",
            name="category",
            field=models.CharField(
                default="CLUB", max_length=50, verbose_name="Категория"
            ),
        ),
        migrations.AddField(
            model_name="activity",
            name="price",
            field=models.IntegerField(default=0, verbose_name="Цена"),
        ),
    ]
