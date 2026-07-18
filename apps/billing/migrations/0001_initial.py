import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("users", "0002_teacherprofile"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="IdempotencyRecord",
            fields=[
                (
                    "key",
                    models.CharField(
                        max_length=36,
                        primary_key=True,
                        serialize=False,
                        verbose_name="Idempotency-Key",
                    ),
                ),
                (
                    "request_fingerprint",
                    models.CharField(
                        max_length=64, verbose_name="Отпечаток запроса (sha256)"
                    ),
                ),
                (
                    "response_status",
                    models.PositiveSmallIntegerField(verbose_name="HTTP-статус ответа"),
                ),
                ("response_body", models.JSONField(verbose_name="Тело ответа")),
                (
                    "locked_until",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        verbose_name="Резервация действительна до",
                    ),
                ),
                (
                    "lock_token",
                    models.UUIDField(
                        blank=True, null=True, verbose_name="Токен владельца резервации"
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True, db_index=True, verbose_name="Создана"
                    ),
                ),
            ],
            options={
                "verbose_name": "Запись идемпотентности",
                "verbose_name_plural": "Записи идемпотентности",
            },
        ),
        migrations.CreateModel(
            name="SubscriptionPlan",
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
                ("name", models.CharField(max_length=255, verbose_name="Название")),
                (
                    "slots_count",
                    models.PositiveSmallIntegerField(verbose_name="Число слотов"),
                ),
                ("price", models.IntegerField(verbose_name="Цена, в копейках")),
                (
                    "base_session_price",
                    models.IntegerField(
                        verbose_name="Базовая цена занятия, в копейках"
                    ),
                ),
            ],
            options={
                "verbose_name": "Тарифный план",
                "verbose_name_plural": "Тарифные планы",
            },
        ),
        migrations.CreateModel(
            name="ParentDeposit",
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
                    "balance",
                    models.IntegerField(default=0, verbose_name="Баланс, в копейках"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="Обновлён"),
                ),
                (
                    "parent",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="deposit",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Родитель",
                    ),
                ),
            ],
            options={
                "verbose_name": "Депозит родителя",
                "verbose_name_plural": "Депозиты родителей",
            },
        ),
        migrations.CreateModel(
            name="Subscription",
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
                    "status",
                    models.CharField(
                        choices=[
                            ("DRAFT", "Черновик"),
                            ("PENDING", "Ожидает оплаты"),
                            ("ACTIVE", "Активен"),
                            ("EXPIRED", "Истёк"),
                            ("CANCELED", "Отменён"),
                        ],
                        db_index=True,
                        default="DRAFT",
                        max_length=20,
                        verbose_name="Статус",
                    ),
                ),
                (
                    "purchase_price",
                    models.IntegerField(verbose_name="Цена покупки, в копейках"),
                ),
                (
                    "base_session_price",
                    models.IntegerField(
                        verbose_name="Базовая цена занятия на момент покупки, в копейках"
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Создан"),
                ),
                (
                    "start_date",
                    models.DateField(
                        blank=True, null=True, verbose_name="Дата первого занятия"
                    ),
                ),
                (
                    "expires_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="Истекает"
                    ),
                ),
                (
                    "parent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="subscriptions",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Родитель",
                    ),
                ),
                (
                    "plan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="subscriptions",
                        to="billing.subscriptionplan",
                        verbose_name="Тариф",
                    ),
                ),
            ],
            options={
                "verbose_name": "Абонемент",
                "verbose_name_plural": "Абонементы",
            },
        ),
        migrations.CreateModel(
            name="Enrollment",
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
                    "slot_id",
                    models.IntegerField(
                        db_index=True,
                        verbose_name="ID слота расписания (домен schedule)",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("HELD", "Бронь до оплаты"),
                            ("ENROLLED", "Записан"),
                            ("CANCELED", "Отменена"),
                        ],
                        db_index=True,
                        default="HELD",
                        max_length=20,
                        verbose_name="Статус",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Создана"),
                ),
                (
                    "student",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="enrollments",
                        to="users.student",
                        verbose_name="Ребёнок",
                    ),
                ),
                (
                    "subscription",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="enrollments",
                        to="billing.subscription",
                        verbose_name="Абонемент",
                    ),
                ),
            ],
            options={
                "verbose_name": "Запись в группу",
                "verbose_name_plural": "Записи в группы",
            },
        ),
        migrations.CreateModel(
            name="SubscriptionSlot",
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
                    "slot_id",
                    models.IntegerField(
                        db_index=True,
                        verbose_name="ID слота расписания (домен schedule)",
                    ),
                ),
                (
                    "granted_tokens",
                    models.PositiveSmallIntegerField(
                        default=4, verbose_name="Выдано фишек"
                    ),
                ),
                (
                    "remaining_tokens",
                    models.PositiveSmallIntegerField(
                        default=4, verbose_name="Остаток фишек"
                    ),
                ),
                (
                    "subscription",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="slots",
                        to="billing.subscription",
                        verbose_name="Абонемент",
                    ),
                ),
            ],
            options={
                "verbose_name": "Слот абонемента",
                "verbose_name_plural": "Слоты абонементов",
            },
        ),
        migrations.CreateModel(
            name="Transaction",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("amount", models.IntegerField(verbose_name="Сумма, в копейках")),
                (
                    "external_id",
                    models.CharField(
                        blank=True,
                        max_length=255,
                        null=True,
                        unique=True,
                        verbose_name="ID платежа ЮКассы",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Ожидает оплаты"),
                            ("SUCCEEDED", "Оплачен"),
                            ("CANCELED", "Отменён"),
                            ("FAILED", "Ошибка сверки"),
                        ],
                        db_index=True,
                        default="PENDING",
                        max_length=20,
                        verbose_name="Статус",
                    ),
                ),
                (
                    "selected_slot_ids",
                    models.JSONField(
                        blank=True,
                        default=list,
                        verbose_name="Выбранные слоты (ID из домена schedule)",
                    ),
                ),
                (
                    "metadata",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        verbose_name="Служебные данные сверки/провайдера (аудит, не очередь)",
                    ),
                ),
                (
                    "requires_compensation",
                    models.BooleanField(
                        default=False, verbose_name="Требуется возврат"
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Создана"),
                ),
                (
                    "parent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="transactions",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Родитель",
                    ),
                ),
                (
                    "subscription",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="transactions",
                        to="billing.subscription",
                        verbose_name="Абонемент",
                    ),
                ),
            ],
            options={
                "verbose_name": "Транзакция",
                "verbose_name_plural": "Транзакции",
            },
        ),
        migrations.CreateModel(
            name="DepositEntry",
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
                    "amount",
                    models.IntegerField(
                        verbose_name="Сумма движения, в копейках (знаковая)"
                    ),
                ),
                (
                    "reason",
                    models.CharField(
                        choices=[
                            (
                                "SUBSCRIPTION_EXPIRY_CREDIT",
                                "Несгораемый остаток абонемента",
                            ),
                            ("CHECKOUT_SPEND", "Списание при покупке"),
                            ("ORDER_CANCELED_RETURN", "Возврат за неисполненный заказ"),
                        ],
                        max_length=40,
                        verbose_name="Основание",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Создана"),
                ),
                (
                    "deposit",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="entries",
                        to="billing.parentdeposit",
                        verbose_name="Депозит",
                    ),
                ),
                (
                    "subscription",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="deposit_entries",
                        to="billing.subscription",
                        verbose_name="Абонемент-источник",
                    ),
                ),
                (
                    "transaction",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="deposit_entries",
                        to="billing.transaction",
                        verbose_name="Транзакция-источник",
                    ),
                ),
            ],
            options={
                "verbose_name": "Движение депозита",
                "verbose_name_plural": "Движения депозита",
            },
        ),
        migrations.CreateModel(
            name="Attendance",
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
                ("date", models.DateField(verbose_name="Дата занятия")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("ATTENDED", "Присутствовал"),
                            ("ABSENT_ERR", "Отсутствие (ошибочная отметка)"),
                            ("ABSENT_OK", "Отсутствие (уважительное)"),
                        ],
                        max_length=20,
                        verbose_name="Статус",
                    ),
                ),
                (
                    "token_debited",
                    models.BooleanField(default=False, verbose_name="Фишка списана"),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Создана"),
                ),
                (
                    "enrollment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="attendances",
                        to="billing.enrollment",
                        verbose_name="Запись",
                    ),
                ),
            ],
            options={
                "verbose_name": "Отметка посещения",
                "verbose_name_plural": "Отметки посещений",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("enrollment", "date"),
                        name="uq_billing_attendance_per_enrollment_date",
                    )
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="parentdeposit",
            constraint=models.CheckConstraint(
                condition=models.Q(("balance__gte", 0)),
                name="ck_billing_deposit_balance_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="enrollment",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status__in", ("HELD", "ENROLLED"))),
                fields=("student", "slot_id"),
                name="uq_billing_active_enrollment_per_student_slot",
            ),
        ),
        migrations.AddConstraint(
            model_name="subscriptionslot",
            constraint=models.CheckConstraint(
                condition=models.Q(("remaining_tokens__gte", 0)),
                name="ck_billing_slot_tokens_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="subscriptionslot",
            constraint=models.UniqueConstraint(
                fields=("subscription", "slot_id"),
                name="uq_billing_slot_per_subscription",
            ),
        ),
        migrations.AddIndex(
            model_name="transaction",
            index=models.Index(
                condition=models.Q(("requires_compensation", True)),
                fields=["created_at"],
                name="ix_billing_tx_refund_fifo",
            ),
        ),
        migrations.AddConstraint(
            model_name="depositentry",
            constraint=models.UniqueConstraint(
                condition=models.Q(("subscription__isnull", False)),
                fields=("subscription", "reason"),
                name="uq_billing_dep_entry_per_sub_reason",
            ),
        ),
        migrations.AddConstraint(
            model_name="depositentry",
            constraint=models.UniqueConstraint(
                condition=models.Q(("transaction__isnull", False)),
                fields=("transaction", "reason"),
                name="uq_billing_dep_entry_per_tx_reason",
            ),
        ),
    ]
