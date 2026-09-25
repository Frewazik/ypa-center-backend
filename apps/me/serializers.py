from __future__ import annotations

from django.db import transaction
from rest_framework import serializers

from apps.billing.models import DepositEntryReason, SubscriptionStatus
from apps.me.services import UpcomingItem
from apps.users.models import (
    PROFILE_REQUIRED_FIELDS,
    Parent,
    ReferralSource,
    Student,
)
from apps.users.consent import (
    ConsentSource,
    grant_registration_consent,
    pd_consent_field,
    require_true,
)

TIME_FORMAT = "%H:%M"
DATE_FORMAT = "%d.%m.%Y"

_NON_DRAFT_STATUS_CHOICES = [
    c for c in SubscriptionStatus.choices if c[0] != SubscriptionStatus.DRAFT
]

# ПОЧЕМУ: UNKNOWN ставит только миграция старым родителям — выбрать его
# в анкете нельзя, иначе вопрос «откуда узнали» теряет смысл
_REFERRAL_INPUT_CHOICES = [
    c for c in ReferralSource.choices if c[0] != ReferralSource.UNKNOWN
]


class ChildSerializer(serializers.ModelSerializer[Student]):
    class Meta:
        model = Student
        fields = ("id", "full_name", "dob", "school_grade", "health_issues")


class ProfileSerializer(serializers.ModelSerializer[Parent]):
    children = ChildSerializer(many=True, read_only=True)
    referral_source = serializers.ChoiceField(
        choices=_REFERRAL_INPUT_CHOICES,
        help_text="Откуда узнали о центре. В ответе у старых родителей бывает UNKNOWN",
    )
    pd_consent = pd_consent_field()
    pd_consent_at = serializers.DateTimeField(
        read_only=True,
        help_text="Когда дано согласие на обработку ПД; null — галочку надо показать",
    )
    profile_completed = serializers.BooleanField(
        source="is_profile_completed",
        read_only=True,
        help_text="false — показать анкету; ЛК и покупки до её заполнения закрыты",
    )

    class Meta:
        model = Parent
        fields = (
            "id",
            "full_name",
            "phone",
            "email",
            "referral_source",
            "pd_consent",
            "pd_consent_at",
            "profile_completed",
            "children",
        )
        read_only_fields = ("email",)
        # ПОЧЕМУ: PATCH частичный — непереданное поле не трогаем, но стереть
        # обязательное поле анкеты нельзя: родитель запер бы себе ЛК
        extra_kwargs = {
            name: {"allow_blank": False}
            for name in PROFILE_REQUIRED_FIELDS
            if name != "referral_source"
        }

    def validate_pd_consent(self, value: bool) -> bool:
        # ПОЧЕМУ: отзыв согласия — отдельный процесс (152-ФЗ ст. 9 ч. 2),
        # не снятие галочки в анкете
        return require_true(value)

    def update(self, instance: Parent, validated_data: dict[str, object]) -> Parent:
        consent = validated_data.pop("pd_consent", False)
        with transaction.atomic():
            parent = super().update(instance, validated_data)
            if consent:
                source = ConsentSource.from_request(self.context["request"])
                grant_registration_consent(parent, source)
        return parent


class SubscriptionSlotViewSerializer(serializers.Serializer):
    schedule_id = serializers.IntegerField(read_only=True)
    activity_name = serializers.CharField(read_only=True)
    group_name = serializers.CharField(read_only=True, allow_blank=True)
    schedule = serializers.CharField(read_only=True)
    remaining_sessions = serializers.IntegerField(read_only=True)
    total_sessions = serializers.IntegerField(read_only=True)


class SubscriptionViewSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    display_id = serializers.CharField(read_only=True)
    status = serializers.ChoiceField(
        choices=_NON_DRAFT_STATUS_CHOICES,
        read_only=True,
    )
    student_name = serializers.CharField(read_only=True, allow_blank=True)
    purchase_price = serializers.IntegerField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    start_date = serializers.DateField(read_only=True, allow_null=True)
    expires_at = serializers.DateTimeField(read_only=True, allow_null=True)
    total_remaining = serializers.IntegerField(read_only=True)
    slots = SubscriptionSlotViewSerializer(many=True, read_only=True)


class TrialViewSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    student_id = serializers.IntegerField(read_only=True)
    student_name = serializers.CharField(read_only=True)
    activity_name = serializers.CharField(read_only=True)
    group_name = serializers.CharField(read_only=True, allow_blank=True)
    trial_date = serializers.DateField(read_only=True)
    start_time = serializers.TimeField(read_only=True, format=TIME_FORMAT)
    end_time = serializers.TimeField(read_only=True, format=TIME_FORMAT)
    status = serializers.CharField(read_only=True)
    # ПОЧЕМУ: null — если платёжная транзакция была аннулирована/потеряна,
    # цену показывать не из чего
    cost = serializers.IntegerField(read_only=True, allow_null=True)
    created_at = serializers.DateTimeField(read_only=True)


class DepositBalanceSerializer(serializers.Serializer):
    balance = serializers.IntegerField(read_only=True)


class DepositEntryViewSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    # Знаковая: плюс — пришло на депозит, минус — потрачено
    amount = serializers.IntegerField(read_only=True)
    reason = serializers.ChoiceField(choices=DepositEntryReason.choices, read_only=True)
    reason_display = serializers.CharField(read_only=True)
    subscription_id = serializers.IntegerField(read_only=True, allow_null=True)
    subscription_display_id = serializers.CharField(read_only=True, allow_null=True)
    created_at = serializers.DateTimeField(read_only=True)


class UpcomingItemSerializer(serializers.Serializer):
    kind = serializers.CharField(read_only=True)
    date = serializers.SerializerMethodField()
    time = serializers.SerializerMethodField()
    student_id = serializers.IntegerField(read_only=True, allow_null=True)
    student_name = serializers.CharField(read_only=True, allow_null=True)
    activity_name = serializers.CharField(read_only=True, allow_null=True)
    group_name = serializers.CharField(
        read_only=True, allow_null=True, allow_blank=True
    )
    title = serializers.CharField(read_only=True, allow_null=True)
    source_type = serializers.CharField(read_only=True)
    source_id = serializers.IntegerField(read_only=True)
    is_rescheduled = serializers.BooleanField(read_only=True)

    def get_date(self, obj: UpcomingItem) -> str:
        return obj.date.strftime(DATE_FORMAT)

    def get_time(self, obj: UpcomingItem) -> str:
        start = obj.start_time.strftime(TIME_FORMAT)
        if obj.end_time is None:
            return start
        return f"{start}-{obj.end_time.strftime(TIME_FORMAT)}"
