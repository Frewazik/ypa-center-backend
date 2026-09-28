from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

# ПОЧЕМУ: до колонок статус и id возврата жили в metadata строчными буквами
_MOVED_KEYS = ("refund_status", "refund_id")


def _metadata_to_columns(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Transaction = apps.get_model("billing", "Transaction")
    for tx in Transaction.objects.filter(metadata__has_any_keys=list(_MOVED_KEYS)):
        metadata = dict(tx.metadata)
        status = metadata.pop("refund_status", None)
        tx.refund_status = str(status).upper() if status else None
        tx.refund_id = metadata.pop("refund_id", None)
        tx.metadata = metadata
        tx.save(update_fields=["refund_status", "refund_id", "metadata"])


def _columns_to_metadata(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Transaction = apps.get_model("billing", "Transaction")
    for tx in Transaction.objects.exclude(refund_status=None, refund_id=None):
        metadata = dict(tx.metadata)
        if tx.refund_status:
            metadata["refund_status"] = tx.refund_status.lower()
        if tx.refund_id:
            metadata["refund_id"] = tx.refund_id
        tx.metadata = metadata
        tx.save(update_fields=["metadata"])


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0009_transaction_received_amount"),
    ]

    operations = [
        migrations.AddField(
            model_name="transaction",
            name="refund_id",
            field=models.CharField(
                blank=True, max_length=64, null=True, verbose_name="ID возврата ЮКассы"
            ),
        ),
        migrations.AddField(
            model_name="transaction",
            name="refund_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("PENDING", "В обработке у ЮКассы"),
                    ("SUCCEEDED", "Выполнен"),
                    ("CANCELED", "Отменён ЮКассой"),
                    ("FAILED", "Не отправлен"),
                    ("MANUAL", "Разобран вручную"),
                ],
                max_length=20,
                null=True,
                verbose_name="Статус возврата",
            ),
        ),
        migrations.RunPython(_metadata_to_columns, _columns_to_metadata),
    ]
