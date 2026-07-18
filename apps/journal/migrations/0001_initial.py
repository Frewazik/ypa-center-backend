import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("schedule", "0004_schedule_age_range"),
    ]

    operations = [
        migrations.CreateModel(
            name="Lesson",
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
                ("date", models.DateField(db_index=True, verbose_name="Дата занятия")),
                (
                    "topic",
                    models.CharField(
                        blank=True, max_length=255, verbose_name="Тема занятия"
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Создано"),
                ),
                (
                    "schedule",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="lessons",
                        to="schedule.schedule",
                        verbose_name="Группа",
                    ),
                ),
            ],
            options={
                "db_table": "lesson",
                "verbose_name": "Занятие",
                "verbose_name_plural": "Занятия",
                "ordering": ("-date",),
            },
        ),
        migrations.AddConstraint(
            model_name="lesson",
            constraint=models.UniqueConstraint(
                fields=("schedule", "date"), name="uq_lesson_per_schedule_date"
            ),
        ),
    ]
