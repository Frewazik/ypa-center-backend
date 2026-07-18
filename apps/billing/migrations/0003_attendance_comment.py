from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        (
            "billing",
            "0002_remove_enrollment_uq_billing_active_enrollment_per_student_slot_and_more",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="attendance",
            name="comment",
            field=models.TextField(blank=True, verbose_name="Комментарий педагога"),
        ),
        migrations.AddField(
            model_name="attendance",
            name="comment_tag",
            field=models.CharField(
                choices=[
                    ("POSITIVE", "Позитивный"),
                    ("NEGATIVE", "Негативный"),
                    ("NEUTRAL", "Нейтральный"),
                ],
                default="NEUTRAL",
                max_length=16,
                verbose_name="Тональность комментария",
            ),
        ),
    ]
