# ПОЧЕМУ: Денормализация time_slot для ExclusionConstraint
# поддерживается триггерами PostgreSQL (0003), а не ORM, чтобы
# инварианты переживали bulk_create и QuerySet.update().
# Требуется btree_gist (0001).

from __future__ import annotations

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import (
    DateTimeRangeField,
    RangeBoundary,
    RangeOperators,
)
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Func, Q

ACTIVITY_MODEL = "catalog.Activity"
TEACHER_MODEL = "users.TeacherProfile"


class DayOfWeek(models.IntegerChoices):
    MONDAY = 0, "Понедельник"
    TUESDAY = 1, "Вторник"
    WEDNESDAY = 2, "Среда"
    THURSDAY = 3, "Четверг"
    FRIDAY = 4, "Пятница"
    SATURDAY = 5, "Суббота"
    SUNDAY = 6, "Воскресенье"


class MaskType(models.TextChoices):
    CANCELLATION = "CANCELLATION", "Отмена"
    RESCHEDULE = "RESCHEDULE", "Перенос"


class AnchoredTimestamp(Func):
    # ПОЧЕМУ: В PostgreSQL нет timerange.
    # Время якорится к константе для работы GiST-индекса

    arity = 1
    template = "(DATE '2000-01-01' + %(expressions)s)"
    output_field = models.DateTimeField()


class TimeOfDayRange(Func):
    # ПОЧЕМУ: Полуоткрытый интервал [start, end) предотвращает
    # ложные коллизии смежных занятий (16:00–17:00 и 17:00–18:00)

    function = "tsrange"
    output_field = DateTimeRangeField()

    def __init__(self, start_field: str, end_field: str) -> None:
        super().__init__(
            AnchoredTimestamp(F(start_field)),
            AnchoredTimestamp(F(end_field)),
            RangeBoundary(),  # '[)'
        )


class Room(models.Model):
    name = models.CharField("Название", max_length=255)
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        db_table = "room"
        verbose_name = "Кабинет"
        verbose_name_plural = "Кабинеты"

    def __str__(self) -> str:
        return self.name

    @property
    def is_bookable(self) -> bool:
        return self.is_active


class TimeSlot(models.Model):
    day_of_week = models.SmallIntegerField("День недели", choices=DayOfWeek.choices)
    start_time = models.TimeField("Начало")
    end_time = models.TimeField("Окончание")

    class Meta:
        db_table = "time_slot"
        verbose_name = "Временной слот"
        verbose_name_plural = "Временные слоты"
        constraints = [
            models.CheckConstraint(
                condition=Q(end_time__gt=F("start_time")),
                name="time_slot_end_after_start",
            ),
            models.CheckConstraint(
                condition=Q(day_of_week__gte=0, day_of_week__lte=6),
                name="time_slot_day_of_week_in_range",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.get_day_of_week_display()} "
            f"{self.start_time:%H:%M}–{self.end_time:%H:%M}"
        )

    def clean(self) -> None:
        if self.start_time is None or self.end_time is None:
            return
        if self.end_time <= self.start_time:
            raise ValidationError(
                {"end_time": "Время окончания должно быть позже времени начала."}
            )


class Schedule(models.Model):
    activity = models.ForeignKey(
        ACTIVITY_MODEL,
        verbose_name="Кружок",
        on_delete=models.PROTECT,
        related_name="slots",
    )
    time_slot = models.ForeignKey(
        TimeSlot,
        verbose_name="Слот",
        on_delete=models.PROTECT,
        related_name="schedules",
    )
    teacher = models.ForeignKey(
        TEACHER_MODEL,
        verbose_name="Преподаватель",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="schedules",
    )
    room = models.ForeignKey(
        Room,
        verbose_name="Кабинет",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="schedules",
    )
    group_name = models.CharField("Название группы", max_length=100, blank=True)
    max_capacity = models.PositiveSmallIntegerField("Максимум детей", default=6)
    is_active = models.BooleanField("Активна", default=True)

    # ПОЧЕМУ: публичная витрина обязана показывать возраст на уровне
    # подгруппы, а не кружка — у одного кружка группы разных возрастов
    age_min = models.PositiveSmallIntegerField("Возраст от", null=True, blank=True)
    age_max = models.PositiveSmallIntegerField("Возраст до", null=True, blank=True)

    # ПОЧЕМУ: Денормализация ради ExclusionConstraint
    # Значения перезаписываются триггером (0003), ORM-хуки игнорируются
    day_of_week = models.SmallIntegerField(
        "День недели (денорм.)", choices=DayOfWeek.choices, editable=False
    )
    start_time = models.TimeField("Начало (денорм.)", editable=False)
    end_time = models.TimeField("Окончание (денорм.)", editable=False)

    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    # ПОЧЕМУ: Колонки в БД нет. Поле заполняется через annotate() в сервисах;
    # аннотация нужна для mypy strict
    capacity_taken: int

    class Meta:
        db_table = "schedule"
        verbose_name = "Группа расписания"
        verbose_name_plural = "Группы расписания"
        constraints = [
            models.CheckConstraint(
                condition=Q(end_time__gt=F("start_time")),
                name="schedule_end_after_start",
            ),
            models.CheckConstraint(
                condition=Q(day_of_week__gte=0, day_of_week__lte=6),
                name="schedule_day_of_week_in_range",
            ),
            models.CheckConstraint(
                condition=Q(age_min__isnull=True)
                | Q(age_max__isnull=True)
                | Q(age_max__gte=F("age_min")),
                name="schedule_age_range_valid",
            ),
            ExclusionConstraint(
                name="no_teacher_time_overlap",
                expressions=[
                    (F("teacher"), RangeOperators.EQUAL),
                    (F("day_of_week"), RangeOperators.EQUAL),
                    (TimeOfDayRange("start_time", "end_time"), RangeOperators.OVERLAPS),
                ],
                condition=Q(is_active=True, teacher__isnull=False),
            ),
            ExclusionConstraint(
                name="no_room_time_overlap",
                expressions=[
                    (F("room"), RangeOperators.EQUAL),
                    (F("day_of_week"), RangeOperators.EQUAL),
                    (TimeOfDayRange("start_time", "end_time"), RangeOperators.OVERLAPS),
                ],
                condition=Q(is_active=True, room__isnull=False),
            ),
        ]

    def __str__(self) -> str:
        return f"Группа #{self.pk} · {self.group_name or self.activity_id}"


class ScheduleMask(models.Model):
    schedule = models.ForeignKey(
        Schedule,
        verbose_name="Группа",
        on_delete=models.CASCADE,
        related_name="masks",
    )
    target_date = models.DateField("Дата занятия", db_index=True)
    type = models.CharField("Тип маски", max_length=20, choices=MaskType.choices)
    new_day_of_week = models.SmallIntegerField(
        "Новый день недели", choices=DayOfWeek.choices, null=True, blank=True
    )
    new_start_time = models.TimeField("Новое начало", null=True, blank=True)
    new_end_time = models.TimeField("Новое окончание", null=True, blank=True)
    new_room = models.ForeignKey(
        Room,
        verbose_name="Новый кабинет",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    new_teacher = models.ForeignKey(
        TEACHER_MODEL,
        verbose_name="Новый преподаватель",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        db_table = "schedule_mask"
        verbose_name = "Маска расписания"
        verbose_name_plural = "Маски расписания"
        constraints = [
            models.UniqueConstraint(
                fields=("schedule", "target_date"),
                name="uniq_mask_per_schedule_per_date",
            ),
            models.CheckConstraint(
                condition=Q(new_day_of_week__isnull=True)
                | Q(new_day_of_week__gte=0, new_day_of_week__lte=6),
                name="schedule_mask_new_day_in_range",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.get_type_display()} {self.target_date:%d.%m.%Y} "
            f"(группа #{self.schedule_id})"
        )

    @property
    def is_cancelled(self) -> bool:
        return self.type == MaskType.CANCELLATION

    def clean(self) -> None:
        # ПОЧЕМУ: Django не вызывает clean() при save().
        # Прямой create() запрещен; сервисы обязаны вызывать full_clean() перед сохранением
        if self.type == MaskType.CANCELLATION:
            self._clean_cancellation()
            return
        if self.type == MaskType.RESCHEDULE:
            self._clean_reschedule()

    def _clean_cancellation(self) -> None:
        overrides = {
            "new_day_of_week": self.new_day_of_week,
            "new_start_time": self.new_start_time,
            "new_end_time": self.new_end_time,
            "new_room": self.new_room_id,
            "new_teacher": self.new_teacher_id,
        }
        filled = [name for name, value in overrides.items() if value is not None]
        if not filled:
            return
        raise ValidationError(
            {name: "Маска-отмена не переопределяет занятие." for name in filled}
        )

    def _clean_reschedule(self) -> None:
        new_start = self.new_start_time
        new_end = self.new_end_time
        errors: dict[str, str] = {}
        if new_start is None:
            errors["new_start_time"] = "Для переноса обязательно новое время начала."
        if new_end is None:
            errors["new_end_time"] = "Для переноса обязательно новое время окончания."
        if new_start is None or new_end is None:
            raise ValidationError(errors)
        if new_end <= new_start:
            raise ValidationError(
                {"new_end_time": "Окончание должно быть позже начала."}
            )
