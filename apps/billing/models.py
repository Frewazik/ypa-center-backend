from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q


class SubscriptionStatus(models.TextChoices):
    DRAFT = "DRAFT", "Черновик"
    PENDING = "PENDING", "Ожидает оплаты"
    ACTIVE = "ACTIVE", "Активен"
    EXPIRED = "EXPIRED", "Истёк"
    CANCELED = "CANCELED", "Отменён"


class TransactionStatus(models.TextChoices):
    PENDING = "PENDING", "Ожидает оплаты"
    SUCCEEDED = "SUCCEEDED", "Оплачен"
    CANCELED = "CANCELED", "Отменён"
    # ПОЧЕМУ: для фатальных ошибок сверки (сумма/валюта); причина уходит в metadata,
    # чтобы не смешивать с пользовательским CANCELED
    FAILED = "FAILED", "Ошибка сверки"


class AttendanceStatus(models.TextChoices):
    ATTENDED = "ATTENDED", "Присутствовал"
    ABSENT_ERR = "ABSENT_ERR", "Отсутствие (ошибочная отметка)"
    ABSENT_OK = "ABSENT_OK", "Отсутствие (уважительное)"


class AttendanceCommentTag(models.TextChoices):
    POSITIVE = "POSITIVE", "Позитивный"
    NEGATIVE = "NEGATIVE", "Негативный"
    NEUTRAL = "NEUTRAL", "Нейтральный"


class SubscriptionPlan(models.Model):
    name = models.CharField("Название", max_length=255)
    slots_count = models.PositiveSmallIntegerField("Число слотов")
    price = models.IntegerField("Цена, в копейках")
    # ПОЧЕМУ: деление price на slots_count дает плавающую копейку;
    # нужна строгая база для возврата на депозит
    base_session_price = models.IntegerField("Базовая цена занятия, в копейках")

    class Meta:
        verbose_name = "Тарифный план"
        verbose_name_plural = "Тарифные планы"

    def __str__(self) -> str:
        return self.name


class Subscription(models.Model):
    parent = models.ForeignKey(
        "users.Parent",
        on_delete=models.PROTECT,
        related_name="subscriptions",
        verbose_name="Родитель",
    )
    plan = models.ForeignKey(
        SubscriptionPlan,
        on_delete=models.PROTECT,
        related_name="subscriptions",
        verbose_name="Тариф",
    )
    status = models.CharField(
        "Статус",
        max_length=20,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.DRAFT,
        db_index=True,
    )
    # ПОЧЕМУ: защита от изменения прайса в будущем; расчет возврата идет по зафиксированным ценам
    purchase_price = models.IntegerField("Цена покупки, в копейках")
    base_session_price = models.IntegerField(
        "Базовая цена занятия на момент покупки, в копейках"
    )
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    # TODO: заполнять при активации через SchedulePort — месяц от первого занятия, не от оплаты.
    start_date = models.DateField("Дата первого занятия", null=True, blank=True)
    expires_at = models.DateTimeField("Истекает", null=True, blank=True)

    class Meta:
        verbose_name = "Абонемент"
        verbose_name_plural = "Абонементы"

    def __str__(self) -> str:
        return f"Subscription #{self.pk} ({self.status})"

    @property
    def is_active(self) -> bool:
        return self.status == SubscriptionStatus.ACTIVE


class SubscriptionSlot(models.Model):
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.CASCADE,
        related_name="slots",
        verbose_name="Абонемент",
    )
    slot_id = models.IntegerField("ID слота расписания (домен schedule)", db_index=True)
    # ПОЧЕМУ: правила выдачи меняются, фиксируем фактическое количество на момент продажи
    granted_tokens = models.PositiveSmallIntegerField("Выдано фишек", default=4)
    remaining_tokens = models.PositiveSmallIntegerField("Остаток фишек", default=4)

    class Meta:
        verbose_name = "Слот абонемента"
        verbose_name_plural = "Слоты абонементов"
        constraints = [
            models.CheckConstraint(
                condition=Q(remaining_tokens__gte=0),
                name="ck_billing_slot_tokens_nonnegative",
            ),
            models.UniqueConstraint(
                fields=["subscription", "slot_id"],
                name="uq_billing_slot_per_subscription",
            ),
        ]

    def __str__(self) -> str:
        return f"SubscriptionSlot #{self.pk} (remaining={self.remaining_tokens})"

    @property
    def is_depleted(self) -> bool:
        return self.remaining_tokens == 0


class Transaction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    parent = models.ForeignKey(
        "users.Parent",
        on_delete=models.PROTECT,
        related_name="transactions",
        verbose_name="Родитель",
    )
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.PROTECT,
        related_name="transactions",
        null=True,
        blank=True,
        verbose_name="Абонемент",
    )
    amount = models.IntegerField("Сумма, в копейках")
    external_id = models.CharField(
        "ID платежа ЮКассы",
        max_length=255,
        null=True,
        blank=True,
        unique=True,
    )
    status = models.CharField(
        "Статус",
        max_length=20,
        choices=TransactionStatus.choices,
        default=TransactionStatus.PENDING,
        db_index=True,
    )
    selected_slot_ids = models.JSONField(
        "Выбранные слоты (ID из домена schedule)",
        default=list,
        blank=True,
    )
    metadata = models.JSONField(
        "Служебные данные сверки/провайдера (аудит, не очередь)",
        default=dict,
        blank=True,
    )
    # ПОЧЕМУ: вынесено из metadata в отдельную колонку, поиск должников по JSONB даст Seq Scan
    requires_compensation = models.BooleanField("Требуется возврат", default=False)
    created_at = models.DateTimeField("Создана", auto_now_add=True)

    class Meta:
        verbose_name = "Транзакция"
        verbose_name_plural = "Транзакции"
        indexes = [
            # ПОЧЕМУ: B-Tree индекс по boolean неэффективен;
            # partial-индекс отсекает только реальных должников
            models.Index(
                fields=["created_at"],
                condition=Q(requires_compensation=True),
                name="ix_billing_tx_refund_fifo",
            ),
        ]

    def __str__(self) -> str:
        return f"Transaction {self.pk} ({self.status})"

    @property
    def is_pending(self) -> bool:
        return self.status == TransactionStatus.PENDING


class EnrollmentStatus(models.TextChoices):
    # ПОЧЕМУ: HELD удерживает место в группе строго на время жизни
    # неоплаченной транзакции (15 минут),
    # защищая от овербукинга до ответа платежного шлюза
    HELD = "HELD", "Бронь до оплаты"
    ENROLLED = "ENROLLED", "Записан"
    CANCELED = "CANCELED", "Отменена"


class Enrollment(models.Model):
    student = models.ForeignKey(
        "users.Student",
        on_delete=models.PROTECT,
        related_name="enrollments",
        verbose_name="Ребёнок",
    )
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.PROTECT,
        related_name="enrollments",
        verbose_name="Абонемент",
    )
    schedule = models.ForeignKey(
        "schedule.Schedule",
        on_delete=models.PROTECT,
        related_name="enrollment",
        verbose_name="Группа",
    )
    status = models.CharField(
        "Статус",
        max_length=20,
        choices=EnrollmentStatus.choices,
        default=EnrollmentStatus.HELD,
        db_index=True,
    )
    created_at = models.DateTimeField("Создана", auto_now_add=True)

    class Meta:
        verbose_name = "Запись в группу"
        verbose_name_plural = "Записи в группы"
        constraints = [
            # ПОЧЕМУ: partial-индекс исключает CANCELED, позволяя купить слот повторно
            # Ловит гонки на уровне БД
            models.UniqueConstraint(
                fields=["student", "schedule"],
                condition=Q(
                    status__in=(EnrollmentStatus.HELD, EnrollmentStatus.ENROLLED)
                ),
                name="uq_billing_active_enrollment_per_student_slot",
            ),
        ]

    def __str__(self) -> str:
        return f"Enrollment #{self.pk} ({self.status})"

    @property
    def is_active(self) -> bool:
        return self.status == EnrollmentStatus.ENROLLED

    @property
    def occupies_seat(self) -> bool:
        return self.status in (EnrollmentStatus.HELD, EnrollmentStatus.ENROLLED)


class Attendance(models.Model):
    enrollment = models.ForeignKey(
        Enrollment,
        on_delete=models.PROTECT,
        related_name="attendances",
        verbose_name="Запись",
    )
    date = models.DateField("Дата занятия")
    status = models.CharField(
        "Статус",
        max_length=20,
        choices=AttendanceStatus.choices,
    )
    token_debited = models.BooleanField("Фишка списана", default=False)
    comment = models.TextField("Комментарий педагога", blank=True)
    comment_tag = models.CharField(
        "Тональность комментария",
        max_length=16,
        choices=AttendanceCommentTag.choices,
        default=AttendanceCommentTag.NEUTRAL,
    )
    created_at = models.DateTimeField("Создана", auto_now_add=True)

    class Meta:
        verbose_name = "Отметка посещения"
        verbose_name_plural = "Отметки посещений"
        constraints = [
            models.UniqueConstraint(
                fields=["enrollment", "date"],
                name="uq_billing_attendance_per_enrollment_date",
            ),
        ]

    def __str__(self) -> str:
        return f"Attendance #{self.pk} ({self.status})"


class IdempotencyRecord(models.Model):
    key = models.CharField("Idempotency-Key", max_length=36, primary_key=True)
    # ПОЧЕМУ: защита от подмены тела запроса при том же Idempotency-Key (возвращает 409)
    request_fingerprint = models.CharField("Отпечаток запроса (sha256)", max_length=64)
    response_status = models.PositiveSmallIntegerField("HTTP-статус ответа")
    response_body = models.JSONField("Тело ответа")
    locked_until = models.DateTimeField(
        "Резервация действительна до", null=True, blank=True
    )
    # ПОЧЕМУ: fencing-токен отсекает зависший процесс, если после таймаута блокировку
    # перехватил другой воркер
    lock_token = models.UUIDField("Токен владельца резервации", null=True, blank=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Запись идемпотентности"
        verbose_name_plural = "Записи идемпотентности"

    def __str__(self) -> str:
        return f"IdempotencyRecord {self.key}"


class DepositEntryReason(models.TextChoices):
    SUBSCRIPTION_EXPIRY_CREDIT = (
        "SUBSCRIPTION_EXPIRY_CREDIT",
        "Несгораемый остаток абонемента",
    )
    CHECKOUT_SPEND = "CHECKOUT_SPEND", "Списание при покупке"
    ORDER_CANCELED_RETURN = "ORDER_CANCELED_RETURN", "Возврат за неисполненный заказ"


class ParentDeposit(models.Model):
    parent = models.OneToOneField(
        "users.Parent",
        on_delete=models.PROTECT,
        related_name="deposit",
        verbose_name="Родитель",
    )
    balance = models.IntegerField("Баланс, в копейках", default=0)
    updated_at = models.DateTimeField("Обновлён", auto_now=True)

    class Meta:
        verbose_name = "Депозит родителя"
        verbose_name_plural = "Депозиты родителей"
        constraints = [
            models.CheckConstraint(
                condition=Q(balance__gte=0),
                name="ck_billing_deposit_balance_nonnegative",
            ),
        ]

    def __str__(self) -> str:
        return f"ParentDeposit #{self.pk} (balance={self.balance})"


class DepositEntry(models.Model):
    # !!! мутация ParentDeposit.balance допускается строго под SELECT FOR UPDATE
    # с одновременным INSERT сюда (аудит)
    deposit = models.ForeignKey(
        ParentDeposit,
        on_delete=models.PROTECT,
        related_name="entries",
        verbose_name="Депозит",
    )
    amount = models.IntegerField("Сумма движения, в копейках (знаковая)")
    reason = models.CharField(
        "Основание",
        max_length=40,
        choices=DepositEntryReason.choices,
    )
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.PROTECT,
        related_name="deposit_entries",
        null=True,
        blank=True,
        verbose_name="Абонемент-источник",
    )
    transaction = models.ForeignKey(
        Transaction,
        on_delete=models.PROTECT,
        related_name="deposit_entries",
        null=True,
        blank=True,
        verbose_name="Транзакция-источник",
    )
    created_at = models.DateTimeField("Создана", auto_now_add=True)

    class Meta:
        verbose_name = "Движение депозита"
        verbose_name_plural = "Движения депозита"
        constraints = [
            # ПОЧЕМУ: гарантирует идемпотентность начислений
            # (защита от двойного списания/возврата при ретраях Taskiq)
            models.UniqueConstraint(
                fields=["subscription", "reason"],
                condition=Q(subscription__isnull=False),
                name="uq_billing_dep_entry_per_sub_reason",
            ),
            models.UniqueConstraint(
                fields=["transaction", "reason"],
                condition=Q(transaction__isnull=False),
                name="uq_billing_dep_entry_per_tx_reason",
            ),
        ]

    def __str__(self) -> str:
        return f"DepositEntry #{self.pk} ({self.reason}: {self.amount})"
