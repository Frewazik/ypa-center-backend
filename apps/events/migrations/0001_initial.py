import django.db.models.deletion
import phonenumber_field.modelfields
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    initial = True

    dependencies: list[tuple[str, str]] = []

    operations = [
        migrations.CreateModel(
            name="Event",
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
                ("title", models.CharField(max_length=255, verbose_name="Название")),
                ("description", models.TextField(blank=True, verbose_name="Описание")),
                (
                    "cover_image",
                    models.URLField(
                        blank=True, max_length=500, verbose_name="Обложка (URL)"
                    ),
                ),
                (
                    "start_datetime",
                    models.DateTimeField(db_index=True, verbose_name="Начало"),
                ),
                (
                    "duration_minutes",
                    models.PositiveSmallIntegerField(
                        default=60, verbose_name="Длительность, мин"
                    ),
                ),
                ("price", models.IntegerField(default=0, verbose_name="Цена")),
                (
                    "capacity",
                    models.PositiveSmallIntegerField(verbose_name="Вместимость"),
                ),
                (
                    "seats_taken",
                    models.PositiveIntegerField(default=0, verbose_name="Занято мест"),
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
                "db_table": "event",
                "verbose_name": "Событие",
                "verbose_name_plural": "События",
                "ordering": ("start_datetime",),
            },
        ),
        migrations.CreateModel(
            name="EventRegistration",
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
                    "child_name",
                    models.CharField(max_length=255, verbose_name="Имя ребёнка"),
                ),
                (
                    "parent_name",
                    models.CharField(max_length=255, verbose_name="Имя родителя"),
                ),
                (
                    "phone",
                    phonenumber_field.modelfields.PhoneNumberField(
                        db_index=True,
                        max_length=128,
                        region="RU",
                        verbose_name="Телефон",
                    ),
                ),
                (
                    "email",
                    models.EmailField(blank=True, max_length=254, verbose_name="Email"),
                ),
                (
                    "attendees_count",
                    models.PositiveSmallIntegerField(
                        default=1, verbose_name="Количество участников"
                    ),
                ),
                (
                    "source",
                    models.CharField(
                        blank=True, max_length=100, verbose_name="Откуда узнали"
                    ),
                ),
                ("comment", models.TextField(blank=True, verbose_name="Комментарий")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("NEW", "Новая"),
                            ("PENDING_PAYMENT", "Ожидает оплаты"),
                            ("CONFIRMED", "Подтверждена"),
                            ("CANCELED", "Отменена"),
                        ],
                        db_index=True,
                        default="NEW",
                        max_length=20,
                        verbose_name="Статус",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Создана"),
                ),
                (
                    "event",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="registrations",
                        to="events.event",
                        verbose_name="Событие",
                    ),
                ),
            ],
            options={
                "db_table": "event_registration",
                "verbose_name": "Регистрация на событие",
                "verbose_name_plural": "Регистрации на события",
                "ordering": ("-created_at",),
            },
        ),
        migrations.AddConstraint(
            model_name="event",
            constraint=models.CheckConstraint(
                condition=Q(("price__gte", 0)), name="event_price_non_negative"
            ),
        ),
        migrations.AddConstraint(
            model_name="event",
            constraint=models.CheckConstraint(
                condition=Q(("capacity__gte", 1)), name="event_capacity_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="eventregistration",
            constraint=models.CheckConstraint(
                condition=Q(("attendees_count__gte", 1)),
                name="event_registration_attendees_positive",
            ),
        ),
        migrations.AddIndex(
            model_name="eventregistration",
            index=models.Index(
                fields=["status", "created_at"], name="event_reg_status_created_idx"
            ),
        ),
    ]
