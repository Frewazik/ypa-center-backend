import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0005_transaction_compensation_claim"),
        ("catalog", "0004_activity_description"),
    ]

    operations = [
        migrations.AddField(
            model_name="enrollment",
            name="type",
            field=models.CharField(
                choices=[
                    ("REGULAR", "Постоянная (абонемент)"),
                    ("TRIAL", "Пробное занятие"),
                ],
                default="REGULAR",
                max_length=20,
                verbose_name="Тип записи",
            ),
        ),
        migrations.AddField(
            model_name="enrollment",
            name="trial_date",
            field=models.DateField(blank=True, null=True, verbose_name="Дата пробного"),
        ),
        migrations.AddField(
            model_name="enrollment",
            name="activity",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="trial_enrollments",
                to="catalog.activity",
                verbose_name="Кружок (для пробного)",
            ),
        ),
        migrations.AlterField(
            model_name="enrollment",
            name="subscription",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="enrollments",
                to="billing.subscription",
                verbose_name="Абонемент",
            ),
        ),
        migrations.AddField(
            model_name="transaction",
            name="enrollment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="transactions",
                to="billing.enrollment",
                verbose_name="Запись (пробное)",
            ),
        ),
        # ПОЧЕМУ порядок условий: Q(**kwargs) сортирует ключи по алфавиту
        # (Q.__init__ → sorted(kwargs.items())). Ручная миграция обязана
        # повторить этот порядок, иначе makemigrations видит фантомные правки
        migrations.AddConstraint(
            model_name="enrollment",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("status__in", ("HELD", "ENROLLED")),
                    ("type", "TRIAL"),
                ),
                fields=("student", "activity"),
                name="uniq_trial_per_student_per_activity",
            ),
        ),
        migrations.AddConstraint(
            model_name="enrollment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("activity__isnull", False),
                        ("subscription__isnull", True),
                        ("trial_date__isnull", False),
                        ("type", "TRIAL"),
                    ),
                    models.Q(
                        ("subscription__isnull", False),
                        ("trial_date__isnull", True),
                        ("type", "REGULAR"),
                    ),
                    _connector="OR",
                ),
                name="ck_billing_enrollment_type_shape",
            ),
        ),
    ]
