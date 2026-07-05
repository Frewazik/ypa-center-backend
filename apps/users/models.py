# Create your models here.
from __future__ import annotations

from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models
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
        """
        Единственная точка создания Parent. Явные аргументы вместо **extra_fields:
        - исключает CWE-915 (Mass Assignment): нет слепой передачи произвольных полей;
        - Mypy strict видит реальный контракт, а не object-обёртку.

        Нормализация email: normalize_email() приводит только доменную часть к нижнему
        регистру. Локальная часть (до @) остаётся case-sensitive по RFC 5321,
        но на практике все крупные провайдеры трактуют её как нечувствительную.
        Дополнительный .lower() на всю строку гарантирует каноничность независимо
        от того, каким путём вызван create_user — через API, Admin, management command
        или фоновую задачу. Defense in Depth: менеджер не доверяет вызывающему коду.
        """
        if not email:
            raise ValueError("Email обязателен")
        normalized = self.normalize_email(email).lower()
        user = self.model(
            email=normalized,
            full_name=full_name,
            is_staff=is_staff,
            is_superuser=is_superuser,
        )
        user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(
        self,
        email: str,
        full_name: str = "",
        password: str | None = None,
    ) -> "Parent":
        """
        Создание суперпользователя — единственное место, где допустим usable-пароль.

        Django Admin аутентифицируется через стандартный ModelBackend, который
        требует check_password(); unusable-пароль отклоняется аппаратно. Без
        этого «чёрного хода» персонал навсегда отрезан от админки (Admin
        Lockout): встроенная админка OTP-флоу не понимает.

        Создание делегируется в create_user (единая точка, unusable-пароль
        по умолчанию), и только при явно переданном пароле он перезаписывается.
        `manage.py createsuperuser` передаёт password сам (интерактивный ввод);
        вызов с password=None (например, --noinput) оставляет пароль unusable —
        его можно задать позже через `manage.py changepassword`.

        Родительский create_user параметр password НЕ принимает намеренно:
        обычный Parent не может получить usable-пароль ни одним путём.
        """
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
    """
    Родитель — основной пользователь системы.
    Вход родителей строго беспарольный (OTP). Пароль принудительно unusable.

    Инвариант «пароль unusable» обеспечивается через ParentManager.create_user —
    единственную точку создания экземпляров. Сервисный слой (services.py)
    обязан использовать create_user явно, а не get_or_create /
    Model(**kwargs).save(), чтобы не обходить менеджер.

    Единственное исключение — staff/superuser: create_superuser может задать
    usable-пароль для входа в Django Admin (ModelBackend требует
    check_password, OTP-флоу админка не понимает). Обычному Parent
    usable-пароль недостижим ни одним путём — create_user параметр password
    не принимает.
    """

    email = models.EmailField(
        verbose_name="Email",
        unique=True,
        # db_index=True убран: unique=True автоматически создаёт уникальный
        # B-tree индекс в PostgreSQL. Дублирующий db_index порождал бы второй
        # идентичный индекс, увеличивая overhead на запись без пользы для чтения.
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
    """Ребёнок, привязанный к профилю родителя."""

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

    def __str__(self) -> str:
        # Доступ к self.parent.email из __str__ инициирует ленивый SQL-запрос
        # при каждом обращении к строковому представлению (N+1 в Admin UI,
        # логировании, shell). __str__ не должен стрелять в базу.
        return self.full_name


class MagicTokens(models.Model):
    """
    Временная запись OTP-кода до верификации.
    После успешной верификации — is_used=True.
    Неверификация атомарно инкрементирует attempts_count.
    """

    email = models.EmailField(
        verbose_name="Email",
        # db_index=True убран: одиночный индекс по email избыточен при наличии
        # двух составных индексов ниже — PostgreSQL их использует для любого
        # запроса, где email стоит первым.
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
            # Имена ≤ 30 символов (Index.max_name_length) — Django роняет
            # импорт модели ValueError'ом на более длинных.
            #
            # Индекс 1: фоновая очистка (services.purge_stale_otp_tokens).
            # B-tree привязан к левому префиксу: очистка идёт по ВСЕЙ таблице,
            # без фильтра по email — email в префиксе делал этот индекс мёртвым
            # для неё (Seq Scan). Равенство по is_used + диапазон по expires_at —
            # точный left-prefix для обеих фаз очистки.
            models.Index(
                fields=["is_used", "expires_at"],
                name="mt_used_expires_idx",
            ),
            # Индекс 2: горячий путь request_otp и verify_otp.
            # request_otp: WHERE email=? ORDER BY created_at DESC LIMIT 1 —
            # фильтра по is_used в запросе НЕТ; is_used посередине индекса
            # ломал бы готовую сортировку и заставлял PostgreSQL вычитывать
            # всю историю токенов email в память и сортировать (filesort).
            # Здесь же взятие первой строки индекса (Index Only Scan для
            # values("created_at")).
            # verify_otp: WHERE email=? AND is_used=False ORDER BY created_at
            # DESC — БД идёт по уже отсортированному хвосту email и отбрасывает
            # is_used=True на лету, без filesort.
            models.Index(
                fields=["email", "-created_at"],
                name="mt_email_created_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"OTP для {self.email} (использован: {self.is_used})"
