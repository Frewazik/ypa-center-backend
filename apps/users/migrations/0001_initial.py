import django.db.models.deletion
import phonenumber_field.modelfields
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.CreateModel(
            name="Parent",
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
                ("password", models.CharField(max_length=128, verbose_name="password")),
                (
                    "last_login",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="last login"
                    ),
                ),
                (
                    "is_superuser",
                    models.BooleanField(
                        default=False,
                        help_text="Designates that this user has all permissions without explicitly assigning them.",
                        verbose_name="superuser status",
                    ),
                ),
                (
                    "email",
                    models.EmailField(
                        max_length=254, unique=True, verbose_name="Email"
                    ),
                ),
                (
                    "full_name",
                    models.CharField(blank=True, max_length=255, verbose_name="ФИО"),
                ),
                (
                    "phone",
                    phonenumber_field.modelfields.PhoneNumberField(
                        blank=True,
                        db_index=True,
                        max_length=128,
                        region="RU",
                        verbose_name="Телефон",
                    ),
                ),
                ("comments", models.TextField(blank=True, verbose_name="Комментарии")),
                (
                    "is_active",
                    models.BooleanField(default=True, verbose_name="Активен"),
                ),
                (
                    "is_staff",
                    models.BooleanField(default=False, verbose_name="Персонал"),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Создан"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="Обновлён"),
                ),
                (
                    "groups",
                    models.ManyToManyField(
                        blank=True,
                        help_text="The groups this user belongs to. A user will get all permissions granted to each of their groups.",
                        related_name="user_set",
                        related_query_name="user",
                        to="auth.group",
                        verbose_name="groups",
                    ),
                ),
                (
                    "user_permissions",
                    models.ManyToManyField(
                        blank=True,
                        help_text="Specific permissions for this user.",
                        related_name="user_set",
                        related_query_name="user",
                        to="auth.permission",
                        verbose_name="user permissions",
                    ),
                ),
            ],
            options={
                "verbose_name": "Родитель",
                "verbose_name_plural": "Родители",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="MagicTokens",
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
                ("email", models.EmailField(max_length=254, verbose_name="Email")),
                ("code", models.CharField(max_length=6, verbose_name="Код")),
                ("expires_at", models.DateTimeField(verbose_name="Действителен до")),
                (
                    "attempts_count",
                    models.PositiveSmallIntegerField(
                        default=0, verbose_name="Попытки ввода"
                    ),
                ),
                (
                    "is_used",
                    models.BooleanField(default=False, verbose_name="Использован"),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Создан"),
                ),
            ],
            options={
                "verbose_name": "OTP-токен",
                "verbose_name_plural": "OTP-токены",
                "indexes": [
                    models.Index(
                        fields=["is_used", "expires_at"], name="mt_used_expires_idx"
                    ),
                    models.Index(
                        fields=["email", "-created_at"], name="mt_email_created_idx"
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="Student",
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
                ("full_name", models.CharField(max_length=255, verbose_name="ФИО")),
                (
                    "school_grade",
                    models.CharField(blank=True, max_length=20, verbose_name="Класс"),
                ),
                ("dob", models.DateField(verbose_name="Дата рождения")),
                (
                    "health_issues",
                    models.TextField(blank=True, verbose_name="Особенности здоровья"),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Создан"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="Обновлён"),
                ),
                (
                    "parent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="children",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Родитель",
                    ),
                ),
            ],
            options={
                "verbose_name": "Ребёнок",
                "verbose_name_plural": "Дети",
                "ordering": ["full_name"],
            },
        ),
    ]
