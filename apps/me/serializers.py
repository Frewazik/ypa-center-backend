from __future__ import annotations

from rest_framework import serializers

from apps.billing.models import SubscriptionStatus
from apps.me.services import UpcomingItem
from apps.users.models import Parent, Student

TIME_FORMAT = "%H:%M"
DATE_FORMAT = "%d.%m.%Y"

_NON_DRAFT_STATUS_CHOICES = [
    c for c in SubscriptionStatus.choices if c[0] != SubscriptionStatus.DRAFT
]


class ChildSerializer(serializers.ModelSerializer[Student]):
    class Meta:
        model = Student
        fields = ("id", "full_name", "dob", "school_grade", "health_issues")


class ProfileSerializer(serializers.ModelSerializer[Parent]):
    children = ChildSerializer(many=True, read_only=True)

    class Meta:
        model = Parent
        fields = ("id", "full_name", "phone", "email", "children")
        read_only_fields = ("email",)


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
