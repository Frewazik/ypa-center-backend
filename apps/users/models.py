from __future__ import annotations

from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models
from django.conf import settings
from phonenumber_field.modelfields import PhoneNumberField

from typing import ClassVar, Final


class ReferralSource(models.TextChoices):
    FRIENDS = "FRIENDS", "Друзья, знакомые"
    SOCIAL = "SOCIAL", "Соцсети (VK, Telegram)"
    MAPS = "MAPS", "Яндекс.Карты, 2ГИС"
    SEARCH = "SEARCH", "Поиск в интернете"
    SIGN = "SIGN", "Вывеска, проходил мимо"
    SCHOOL = "SCHOOL", "Школа, детский сад"
    OTHER = "OTHER", "Другое"
    # ПОЧЕМУ: проставляется миграцией родителям, зарегистрированным до
    # появления поля, — источник у них честно неизвестен
    UNKNOWN = "UNKNOWN", "Не указано"


class ConsentPurpose(models.TextChoices):
    REGISTRATION = "REGISTRATION", "Регистрация в личном кабинете"
    CALLBACK = "CALLBACK", "Заказ обратного звонка"
    FEEDBACK = "FEEDBACK", "Обратная связь"
    EVENT_REGISTRATION = "EVENT_REGISTRATION", "Запись на событие"


# ПОЧЕМУ: единственный источник правды правила «анкета заполнена» (вместе
# с согласием на обработку ПД) — его читают permission-класс, verify
# и сериализатор профиля
PROFILE_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "full_name",
    "phone",
    "referral_source",
)


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
    referral_source = models.CharField(
        verbose_name="Откуда узнали",
        max_length=32,
        choices=ReferralSource.choices,
        blank=True,
        default="",
    )
    # ПОЧЕМУ: состояние для проверки анкеты без лишнего запроса. Доказательство
    # (версия документа, IP, браузер) — в журнале PersonalDataConsent
    pd_consent_at = models.DateTimeField(
        verbose_name="Согласие на обработку ПД",
        null=True,
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

    @property
    def is_profile_completed(self) -> bool:
        # ПОЧЕМУ: вычисляется на лету, а не хранится флагом — флаг расходится
        # с полями, когда их правят в админке. Поля лежат в той же строке,
        # что уже загрузил JWTAuthentication, лишних запросов нет
        has_fields = all(
            str(getattr(self, name)).strip() for name in PROFILE_REQUIRED_FIELDS
        )
        return has_fields and self.pd_consent_at is not None


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


class PersonalDataConsent(models.Model):
    """Журнал согласий на обработку ПД (152-ФЗ, ст. 9).

    Обязанность доказать, что согласие получено, лежит на операторе
    (ст. 9 ч. 3). Строка — это доказательство: кто, когда, на какую версию
    документа и откуда согласился. Только добавление: записи не правятся
    и не удаляются, отзыв согласия — отдельная будущая запись.
    """

    purpose = models.CharField(
        verbose_name="Где дано",
        max_length=32,
        choices=ConsentPurpose.choices,
    )
    document_version = models.CharField(verbose_name="Версия документа", max_length=32)
    # ПОЧЕМУ: SET_NULL, а не CASCADE — удаление аккаунта не должно
    # уничтожать доказательство, что согласие было; контакт остаётся снимком
    parent = models.ForeignKey(
        Parent,
        verbose_name="Родитель",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pd_consents",
        # Поиск по родителю покрывает составной индекс pdc_parent_created_idx
        db_index=False,
    )
    email = models.EmailField(verbose_name="Email", blank=True)
    phone = PhoneNumberField(verbose_name="Телефон", region="RU", blank=True)
    # ПОЧЕМУ: id заявки/регистрации в таблице, которую задаёт purpose.
    # Без FK — журнал общий для четырёх таблиц и переживает их чистку
    source_id = models.PositiveBigIntegerField(
        verbose_name="ID заявки", null=True, blank=True
    )
    ip = models.GenericIPAddressField(verbose_name="IP", null=True, blank=True)
    user_agent = models.CharField(verbose_name="Браузер", max_length=512, blank=True)
    created_at = models.DateTimeField(verbose_name="Дано", auto_now_add=True)

    class Meta:
        verbose_name = "Согласие на обработку ПД"
        verbose_name_plural = "Согласия на обработку ПД"
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["parent", "-created_at"], name="pdc_parent_created_idx"
            ),
            models.Index(
                fields=["purpose", "source_id"], name="pdc_purpose_source_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_purpose_display()} · {self.document_version}"


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
