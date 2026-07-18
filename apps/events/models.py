from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.utils import timezone
from phonenumber_field.modelfields import PhoneNumberField


class RegistrationStatus(models.TextChoices):
    NEW = "NEW", "Новая"
    PENDING_PAYMENT = "PENDING_PAYMENT", "Ожидает оплаты"
    CONFIRMED = "CONFIRMED", "Подтверждена"
    CANCELED = "CANCELED", "Отменена"


SEAT_BLOCKING_STATUSES: tuple[str, ...] = (
    RegistrationStatus.NEW,
    RegistrationStatus.PENDING_PAYMENT,
    RegistrationStatus.CONFIRMED,
)


class Event(models.Model):
    title = models.CharField("Название", max_length=255)
    description = models.TextField("Описание", blank=True)
    cover_image = models.URLField("Обложка (URL)", max_length=500, blank=True)
    start_datetime = models.DateTimeField("Начало", db_index=True)
    duration_minutes = models.PositiveSmallIntegerField("Длительность, мин", default=60)
    # ПОЧЕМУ: цена в копейках, как у catalog.Activity (README §5); 0 = бесплатно
    price = models.IntegerField("Цена", default=0)
    capacity = models.PositiveSmallIntegerField("Вместимость")
    # ПОЧЕМУ: денормализованный счётчик вместо SUM по регистрациям —
    # агрегация под select_for_update удерживала бы лок на время I/O.
    # Мутируется ТОЛЬКО сервисами events под локом события
    seats_taken = models.PositiveIntegerField("Занято мест", default=0)
    is_published = models.BooleanField("Опубликовано", default=False)
    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        db_table = "event"
        verbose_name = "Событие"
        verbose_name_plural = "События"
        ordering = ("start_datetime",)
        constraints = [
            models.CheckConstraint(
                condition=Q(price__gte=0),
                name="event_price_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(capacity__gte=1),
                name="event_capacity_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.start_datetime:%d.%m.%Y %H:%M})"

    @property
    def is_free(self) -> bool:
        return self.price == 0

    @property
    def is_upcoming(self) -> bool:
        return self.start_datetime > timezone.now()

    @property
    def seats_free(self) -> int:
        return max(self.capacity - self.seats_taken, 0)


class EventRegistration(models.Model):
    event = models.ForeignKey(
        Event,
        verbose_name="Событие",
        on_delete=models.PROTECT,
        related_name="registrations",
    )
    # ПОЧЕМУ: регистрация анонимна, но если гость был авторизован —
    # привязываем к профилю; гостевые дотягиваются в ЛК по номеру телефона
    parent = models.ForeignKey(
        "users.Parent",
        verbose_name="Родитель",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="event_registrations",
    )
    child_name = models.CharField("Имя ребёнка", max_length=255)
    parent_name = models.CharField("Имя родителя", max_length=255)
    phone = PhoneNumberField("Телефон", region="RU", db_index=True)
    email = models.EmailField("Email", blank=True)
    attendees_count = models.PositiveSmallIntegerField(
        "Количество участников", default=1
    )
    source = models.CharField("Откуда узнали", max_length=100, blank=True)
    comment = models.TextField("Комментарий", blank=True)
    status = models.CharField(
        "Статус",
        max_length=20,
        choices=RegistrationStatus.choices,
        default=RegistrationStatus.NEW,
        db_index=True,
    )
    created_at = models.DateTimeField("Создана", auto_now_add=True)

    class Meta:
        db_table = "event_registration"
        verbose_name = "Регистрация на событие"
        verbose_name_plural = "Регистрации на события"
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(
                condition=Q(attendees_count__gte=1),
                name="event_registration_attendees_positive",
            ),
        ]
        indexes = [
            models.Index(
                fields=["status", "created_at"],
                name="event_reg_status_created_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Регистрация #{self.pk} на «{self.event}» ({self.status})"

    @property
    def occupies_seats(self) -> bool:
        return self.status in SEAT_BLOCKING_STATUSES
