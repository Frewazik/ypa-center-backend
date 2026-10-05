# ПОЧЕМУ: защита от IDOR, parent_id из тела запроса игнорируется,
# владелец вычисляется строго на уровне view через токен авторизации
from __future__ import annotations

from typing import Literal

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


class CheckoutTrialSerializer(serializers.Serializer):
    student_id = serializers.IntegerField(min_value=1)
    schedule_id = serializers.IntegerField(min_value=1)
    # ПОЧЕМУ: у пробного, в отличие от абонемента, дата настоящая календарная,
    # а не паттерн сетки (checkout-flow.md §2)
    trial_date = serializers.DateField()


class CheckoutResponseSerializer(serializers.Serializer):
    transaction_id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=("PENDING_PAYMENT", "CONFIRMED"))
    # ПОЧЕМУ: может быть null, если заказ покрыт депозитом
    # и внешняя ссылка на оплату не формировалась
    payment_url = serializers.URLField(allow_null=True)
    # ПОЧЕМУ: может быть null при CONFIRMED, так как при оплате депозитом
    # счет в кассе не создается и таймера истечения нет
    expires_at = serializers.DateTimeField(allow_null=True)


_TIME_FORMAT = "%H:%M"
# ПОЧЕМУ публичные: на них ссылается SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"] —
# оба экрана результата (свой и гостевой) отдают один и тот же набор
CHECKOUT_OUTCOME_STATUS_CHOICES = ("PENDING", "SUCCEEDED", "CANCELED", "REFUND")
REFUND_REASON_CHOICES = (
    "SEATS_TAKEN",
    "GROUP_CLOSED",
    "PAID_AFTER_EXPIRY",
    "AMOUNT_MISMATCH",
    "CANCELED_BY_CENTER",
    "NOT_FULFILLED",
)
_AMOUNT_HELP = (
    "Копейки: сколько прошло через карту (оно же вернётся), "
    "до оплаты — сколько предстоит заплатить"
)
_EXPIRES_HELP = "До какого момента ждём оплату; осмысленно только для PENDING"


class CheckoutOrderSlotSerializer(serializers.Serializer):
    schedule_id = serializers.IntegerField()
    activity_name = serializers.CharField()
    group_name = serializers.CharField(allow_blank=True)
    day_of_week = serializers.IntegerField(help_text="0 — понедельник, 6 — воскресенье")
    start_time = serializers.TimeField(format=_TIME_FORMAT)
    end_time = serializers.TimeField(format=_TIME_FORMAT)


class CheckoutOrderSerializer(serializers.Serializer):
    title = serializers.CharField()
    student_name = serializers.CharField(allow_blank=True)
    trial_date = serializers.DateField(allow_null=True)
    slots = CheckoutOrderSlotSerializer(many=True)


class CheckoutTransactionSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    type = serializers.ChoiceField(choices=("SUBSCRIPTION", "TRIAL"))
    # ПОЧЕМУ: исход для родителя, а не статус строки в БД — оплаченное пробное
    # без места в БД SUCCEEDED, а для родителя это возврат
    status = serializers.ChoiceField(choices=CHECKOUT_OUTCOME_STATUS_CHOICES)
    # ПОЧЕМУ: заполнен только у REFUND. У CANCELED null — причину отказа
    # банка адаптер ЮКассы пока не разбирает
    reason = serializers.ChoiceField(choices=REFUND_REASON_CHOICES, allow_null=True)
    amount = serializers.IntegerField(help_text=_AMOUNT_HELP)
    created_at = serializers.DateTimeField()
    expires_at = serializers.DateTimeField(help_text=_EXPIRES_HELP)
    order = CheckoutOrderSerializer()


class EventCheckoutOrderSerializer(serializers.Serializer):
    # ПОЧЕМУ без имён и контактов: ответ отдаётся по одному id без входа
    event_id = serializers.IntegerField()
    title = serializers.CharField()
    starts_at = serializers.DateTimeField()
    attendees_count = serializers.IntegerField()


class EventCheckoutTransactionSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    type = serializers.SerializerMethodField()
    status = serializers.ChoiceField(choices=CHECKOUT_OUTCOME_STATUS_CHOICES)
    reason = serializers.ChoiceField(choices=REFUND_REASON_CHOICES, allow_null=True)
    amount = serializers.IntegerField(help_text=_AMOUNT_HELP)
    created_at = serializers.DateTimeField()
    expires_at = serializers.DateTimeField(help_text=_EXPIRES_HELP)
    order = EventCheckoutOrderSerializer()

    def get_type(self, obj: object) -> Literal["EVENT"]:
        return "EVENT"


class _YookassaPaymentObjectSerializer(serializers.Serializer):
    id = serializers.RegexField(regex=r"^[A-Za-z0-9\-]{1,64}$")


class YookassaWebhookSerializer(serializers.Serializer):
    # ПОЧЕМУ: мы не доверяем payload вебхука из соображений безопасности,
    # извлекаем строго ID платежа для последующего синхронного запроса в API

    event = serializers.CharField(max_length=64)
    object = _YookassaPaymentObjectSerializer()
