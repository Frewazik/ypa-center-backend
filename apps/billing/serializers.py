# ПОЧЕМУ: защита от IDOR, parent_id из тела запроса игнорируется,
# владелец вычисляется строго на уровне view через токен авторизации
from __future__ import annotations

from rest_framework import serializers


class CheckoutSubscriptionSerializer(serializers.Serializer):
    plan_id = serializers.IntegerField(min_value=1)
    student_id = serializers.IntegerField(min_value=1)
    use_deposit = serializers.BooleanField(required=False, default=False)
    slot_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        max_length=10,
    )

    def validate_slot_ids(self, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise serializers.ValidationError("Слоты не должны повторяться.")
        return value


class CheckoutResponseSerializer(serializers.Serializer):
    transaction_id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=("PENDING_PAYMENT", "CONFIRMED"))
    # ПОЧЕМУ: может быть null, если заказ покрыт депозитом
    # и внешняя ссылка на оплату не формировалась
    payment_url = serializers.URLField(allow_null=True)
    # ПОЧЕМУ: может быть null при CONFIRMED, так как при оплате депозитом
    # счет в кассе не создается и таймера истечения нет
    expires_at = serializers.DateTimeField(allow_null=True)


class _YookassaPaymentObjectSerializer(serializers.Serializer):
    id = serializers.RegexField(regex=r"^[A-Za-z0-9\-]{1,64}$")


class YookassaWebhookSerializer(serializers.Serializer):
    # ПОЧЕМУ: мы не доверяем payload вебхука из соображений безопасности,
    # извлекаем строго ID платежа для последующего синхронного запроса в API

    event = serializers.CharField(max_length=64)
    object = _YookassaPaymentObjectSerializer()
