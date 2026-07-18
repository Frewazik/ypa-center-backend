from __future__ import annotations

from rest_framework import serializers

from apps.users.models import Parent, Student

TIME_FORMAT = "%H:%M"


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
    day_of_week = serializers.IntegerField(read_only=True)
    start_time = serializers.TimeField(read_only=True, format=TIME_FORMAT)
    end_time = serializers.TimeField(read_only=True, format=TIME_FORMAT)
    student_id = serializers.IntegerField(read_only=True)
    student_name = serializers.CharField(read_only=True)
    remaining_sessions = serializers.IntegerField(read_only=True)


class SubscriptionViewSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    display_id = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    student_name = serializers.CharField(read_only=True, allow_blank=True)
    purchase_price = serializers.IntegerField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    start_date = serializers.DateField(read_only=True, allow_null=True)
    expires_at = serializers.DateTimeField(read_only=True, allow_null=True)
    slots = SubscriptionSlotViewSerializer(many=True, read_only=True)


class UpcomingItemSerializer(serializers.Serializer):
    kind = serializers.CharField(read_only=True)
    date = serializers.DateField(read_only=True)
    start_time = serializers.TimeField(read_only=True, format=TIME_FORMAT)
    end_time = serializers.TimeField(
        read_only=True, format=TIME_FORMAT, allow_null=True
    )
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
