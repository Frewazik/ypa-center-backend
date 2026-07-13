from __future__ import annotations

from django.db import models
from phonenumber_field.modelfields import PhoneNumberField
from simple_history.models import HistoricalRecords


class CallTimeWindow(models.TextChoices):
    MORNING = "MORNING", "Утро (9:00–12:00)"
    AFTERNOON = "AFTERNOON", "День (12:00–17:00)"
    EVENING = "EVENING", "Вечер (17:00–21:00)"


class CallbackStatus(models.TextChoices):
    NEW = "NEW", "Новая"
    IN_PROGRESS = "IN_PROGRESS", "В работе"
    DONE = "DONE", "Обработана"
    SPAM = "SPAM", "Спам"


class FeedbackStatus(models.TextChoices):
    NEW = "NEW", "Новое"
    REVIEWED = "REVIEWED", "Рассмотрено"
    SPAM = "SPAM", "Спам"


class CallbackRequest(models.Model):
    name = models.CharField("Имя", max_length=255)
    # ПОЧЕМУ: E.164 даёт единый канонический формат для поиска
    # в админке и будущей отправки SMS
    phone = PhoneNumberField("Телефон", region="RU", db_index=True)
    preferred_time_window = models.CharField(
        "Удобное время звонка",
        max_length=20,
        choices=CallTimeWindow.choices,
    )
    status = models.CharField(
        "Статус обработки",
        max_length=20,
        choices=CallbackStatus.choices,
        default=CallbackStatus.NEW,
        db_index=True,
    )
    created_at = models.DateTimeField("Создана", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("Обновлена", auto_now=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "Заявка на обратный звонок"
        verbose_name_plural = "Заявки на обратный звонок"
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"Звонок {self.phone} ({self.get_preferred_time_window_display()})"


class FeedbackRequest(models.Model):
    name = models.CharField("Имя", max_length=255, blank=True, default="")
    email = models.EmailField("Email", db_index=True)
    message = models.TextField("Сообщение")
    status = models.CharField(
        "Статус обработки",
        max_length=20,
        choices=FeedbackStatus.choices,
        default=FeedbackStatus.NEW,
        db_index=True,
    )
    created_at = models.DateTimeField("Создано", auto_now_add=True, db_index=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "Обращение с сайта"
        verbose_name_plural = "Обращения с сайта"
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"Обращение от {self.email}"