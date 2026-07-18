from __future__ import annotations

from django.db import models

SCHEDULE_MODEL = "schedule.Schedule"


class Lesson(models.Model):
    schedule = models.ForeignKey(
        SCHEDULE_MODEL,
        verbose_name="Группа",
        on_delete=models.PROTECT,
        related_name="lessons",
    )
    date = models.DateField("Дата занятия", db_index=True)
    # ПОЧЕМУ: тема пишется учителем, чтобы видеть, что именно пропустил
    # отсутствовавший ученик
    topic = models.CharField("Тема занятия", max_length=255, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        db_table = "lesson"
        verbose_name = "Занятие"
        verbose_name_plural = "Занятия"
        ordering = ("-date",)
        constraints = [
            models.UniqueConstraint(
                fields=("schedule", "date"),
                name="uq_lesson_per_schedule_date",
            ),
        ]

    def __str__(self) -> str:
        return f"Занятие {self.date:%d.%m.%Y} (группа #{self.schedule_id})"
