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
    is_unlimited = models.BooleanField("Безлимит", default=False)
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Тарифный план"
        verbose_name_plural = "Тарифные планы"

    def __str__(self) -> str:
        return self.name

    @property
    def price_per_session(self) -> int | None:
        if self.is_unlimited or not self.slots_count:
            return None
        return round(self.price / self.slots_count)


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
    # ПОЧЕМУ: транзакция пробного не связана с абонементом — вебхук находит,
    # что подтверждать, по прямой ссылке на бронь (у абонемента здесь NULL)
    enrollment = models.ForeignKey(
        "Enrollment",
        on_delete=models.PROTECT,
        related_name="transactions",
        null=True,
        blank=True,
        verbose_name="Запись (пробное)",
    )
    # ПОЧЕМУ: вынесено из metadata в отдельную колонку, поиск должников по JSONB даст Seq Scan
    requires_compensation = models.BooleanField("Требуется возврат", default=False)
    # ПОЧЕМУ: lease-резервация возврата (claim check) — параллельный тик
    # процессора компенсаций не должен отправить второй create_refund;
    # TTL вместо вечного флага, чтобы возврат упавшего воркера не завис навсегда
    compensation_claimed_until = models.DateTimeField(
        "Возврат зарезервирован до", null=True, blank=True
    )
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


class EnrollmentType(models.TextChoices):
    REGULAR = "REGULAR", "Постоянная (абонемент)"
    TRIAL = "TRIAL", "Пробное занятие"


class Enrollment(models.Model):
    student = models.ForeignKey(
        "users.Student",
        on_delete=models.PROTECT,
        related_name="enrollments",
        verbose_name="Ребёнок",
    )
    # ПОЧЕМУ nullable: у пробного нет абонемента; форма строки охраняется
    # ck_billing_enrollment_type_shape
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.PROTECT,
        related_name="enrollments",
        null=True,
        blank=True,
        verbose_name="Абонемент",
    )
    schedule = models.ForeignKey(
        "schedule.Schedule",
        on_delete=models.PROTECT,
        related_name="enrollment",
        verbose_name="Группа",
    )
    type = models.CharField(
        "Тип записи",
        max_length=20,
        choices=EnrollmentType.choices,
        default=EnrollmentType.REGULAR,
    )
    # ПОЧЕМУ: пробное — разовый визит на конкретную календарную дату,
    # в отличие от регулярной записи, живущей в абсолютной сетке
    trial_date = models.DateField("Дата пробного", null=True, blank=True)
    # ПОЧЕМУ денормализация: лимит «1 пробное на ребёнка по кружку» действует
    # на уровне кружка, а запись ссылается на слот; без своей колонки
    # partial-unique в БД невозможен (schema-audit R7)
    activity = models.ForeignKey(
        "catalog.Activity",
        on_delete=models.PROTECT,
        related_name="trial_enrollments",
        null=True,
        blank=True,
        verbose_name="Кружок (для пробного)",
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
            # ПОЧЕМУ только REGULAR: пробное остаётся ENROLLED и после визита —
            # без фильтра по типу оно навсегда закрывало абонемент в ту же группу.
            # Обратный запрет (пробное поверх абонемента) — в create_trial_payment
            models.UniqueConstraint(
                fields=["student", "schedule"],
                condition=Q(
                    type=EnrollmentType.REGULAR,
                    status__in=(EnrollmentStatus.HELD, EnrollmentStatus.ENROLLED),
                ),
                name="uq_billing_active_regular_per_student_slot",
            ),
            # ПОЧЕМУ: инвариант №5 (project-context §13) — максимум 1 пробное
            # на ребёнка по кружку. CANCELED не в условии: сорванная оплата
            # не должна сжигать лимит навсегда
            models.UniqueConstraint(
                fields=["student", "activity"],
                condition=Q(
                    type=EnrollmentType.TRIAL,
                    status__in=(EnrollmentStatus.HELD, EnrollmentStatus.ENROLLED),
                ),
                name="uniq_trial_per_student_per_activity",
            ),
            # ПОЧЕМУ: форма строки по типу — пробное обязано иметь дату и кружок
            # и не иметь абонемента; регулярная запись — ровно наоборот
            models.CheckConstraint(
                condition=(
                    Q(
                        type=EnrollmentType.TRIAL,
                        trial_date__isnull=False,
                        activity__isnull=False,
                        subscription__isnull=True,
                    )
                    | Q(
                        type=EnrollmentType.REGULAR,
                        trial_date__isnull=True,
                        subscription__isnull=False,
                    )
                ),
                name="ck_billing_enrollment_type_shape",
            ),
        ]
        indexes = [
            # ПОЧЕМУ partial: пробных на порядки меньше регулярных записей,
            # а занятость всегда спрашивают парой (слот, дата) и только по
            # живым броням — узкий индекс вместо сканирования всех записей
            models.Index(
                fields=["schedule", "trial_date"],
                condition=Q(
                    type=EnrollmentType.TRIAL,
                    status__in=(EnrollmentStatus.HELD, EnrollmentStatus.ENROLLED),
                ),
                name="ix_enroll_trial_slot_date",
            ),
        ]

    def __str__(self) -> str:
        return f"Enrollment #{self.pk} ({self.type}, {self.status})"

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
