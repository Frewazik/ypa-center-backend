from __future__ import annotations

from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models
from django.conf import settings
from phonenumber_field.modelfields import PhoneNumberField

from typing import ClassVar


class ParentManager(BaseUserManager["Parent"]):
    def create_user(
        self,
        email: str,
        full_name: str = "",
        is_staff: bool = False,
        is_superuser: bool = False,
    ) -> "Parent":
        # ПОЧЕМУ: принудительный .lower() гарантирует каноничность email во всей системе
        if not email:
            raise ValueError("Email обязателен")
        normalized = self.normalize_email(email).lower()
        user = self.model(
            email=normalized,
            full_name=full_name,
            is_staff=is_staff,
            is_superuser=is_superuser,
        )
        # ПОЧЕМУ: у обычных родителей нет пароля, аутентификация строго через OTP
        user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(
        self,
        email: str,
        full_name: str = "",
        password: str | None = None,
    ) -> "Parent":
        # ПОЧЕМУ: суперпользователю необходим usable-пароль для входа в Django Admin,
        # так как она не поддерживает OTP
        user = self.create_user(
            email=email,
            full_name=full_name,
            is_staff=True,
            is_superuser=True,
        )
        if password:
            user.set_password(password)
            user.save(update_fields=["password"])
        return user


class Parent(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(
        verbose_name="Email",
        unique=True,
    )
    full_name = models.CharField(
        verbose_name="ФИО",
        max_length=255,
        blank=True,
    )
    phone = PhoneNumberField(
        verbose_name="Телефон",
        region="RU",
        blank=True,
        db_index=True,
    )
    comments = models.TextField(
        verbose_name="Комментарии",
        blank=True,
    )
    is_active = models.BooleanField(verbose_name="Активен", default=True)
    is_staff = models.BooleanField(verbose_name="Персонал", default=False)
    created_at = models.DateTimeField(verbose_name="Создан", auto_now_add=True)
    updated_at = models.DateTimeField(verbose_name="Обновлён", auto_now=True)

    objects: ClassVar[ParentManager] = ParentManager()

    USERNAME_FIELD: ClassVar[str] = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta:
        verbose_name = "Родитель"
        verbose_name_plural = "Родители"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.email


class Student(models.Model):
    parent = models.ForeignKey(
        Parent,
        verbose_name="Родитель",
        on_delete=models.CASCADE,
        related_name="children",
    )
    full_name = models.CharField(verbose_name="ФИО", max_length=255)
    school_grade = models.CharField(
        verbose_name="Класс",
        max_length=20,
        blank=True,
    )
    dob = models.DateField(verbose_name="Дата рождения")
    health_issues = models.TextField(
        verbose_name="Особенности здоровья",
        blank=True,
    )
    created_at = models.DateTimeField(verbose_name="Создан", auto_now_add=True)
    updated_at = models.DateTimeField(verbose_name="Обновлён", auto_now=True)

    class Meta:
        verbose_name = "Ребёнок"
        verbose_name_plural = "Дети"
        ordering = ["full_name"]
        constraints = [
            # ПОЧЕМУ: ловит дабл-сабмит формы. Близнецов различает дата
            # рождения + разные имена; полный тёзка с той же датой у одного
            # родителя в реальности не встречается
            models.UniqueConstraint(
                fields=("parent", "full_name", "dob"),
                name="uq_student_per_parent_name_dob",
            ),
        ]

    def __str__(self) -> str:
        return self.full_name


class MagicTokens(models.Model):
    email = models.EmailField(
        verbose_name="Email",
    )
    code = models.CharField(
        verbose_name="Код",
        max_length=6,
    )
    expires_at = models.DateTimeField(verbose_name="Действителен до")
    attempts_count = models.PositiveSmallIntegerField(
        verbose_name="Попытки ввода",
        default=0,
    )
    is_used = models.BooleanField(verbose_name="Использован", default=False)
    created_at = models.DateTimeField(verbose_name="Создан", auto_now_add=True)

    class Meta:
        verbose_name = "OTP-токен"
        verbose_name_plural = "OTP-токены"
        indexes = [
            # ПОЧЕМУ: составной индекс для эффективной работы фонового таска очистки протухших токенов
            models.Index(
                fields=["is_used", "expires_at"],
                name="mt_used_expires_idx",
            ),
            # ПОЧЕМУ: составной индекс с сортировкой для горячего пути
            # выборки последнего OTP без filesort в Postgres
            models.Index(
                fields=["email", "-created_at"],
                name="mt_email_created_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"OTP для {self.email} (использован: {self.is_used})"


class TeacherProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="teacher_profile",
    )
    middle_name = models.CharField("Отчество", max_length=100, blank=True)
    photo_url = models.URLField("Фото (URL)", max_length=500, blank=True)
    position = models.CharField("Должность на витрине", max_length=150, blank=True)
    quote = models.CharField("Цитата", max_length=255, blank=True)
    bio = models.TextField("О преподавателе", blank=True)

    class Meta:
        db_table = "teacher_profile"
