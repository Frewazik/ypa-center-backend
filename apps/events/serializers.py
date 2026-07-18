from __future__ import annotations

from rest_framework import serializers

from apps.events.models import EventRegistration


def _honeypot_field() -> serializers.CharField:
    # ПОЧЕМУ: поле скрыто через CSS на фронте, живой человек его
    # не видит — заполнит только бот
    return serializers.CharField(
        required=False, allow_blank=True, write_only=True, default=""
    )


class EventRegistrationCreateSerializer(serializers.ModelSerializer[EventRegistration]):
    website_url = _honeypot_field()
    attendees_count = serializers.IntegerField(min_value=1, max_value=20, default=1)

    class Meta:
        model = EventRegistration
        fields = (
            "child_name",
            "parent_name",
            "phone",
            "email",
            "attendees_count",
            "source",
            "comment",
            "website_url",
        )


class RegistrationAcceptedSerializer(serializers.Serializer[dict[str, str]]):
    status = serializers.CharField()
