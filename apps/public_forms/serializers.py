from __future__ import annotations

from rest_framework import serializers

from apps.public_forms.models import CallbackRequest, FeedbackRequest


def _honeypot_field() -> serializers.CharField:
    # ПОЧЕМУ: поле скрыто через CSS на фронте, живой человек его
    # не видит — заполнит только бот
    return serializers.CharField(required=False, allow_blank=True, write_only=True, default="")


def _captcha_field() -> serializers.CharField:
    # ПОЧЕМУ: жёсткий required выдал бы honeypot-ловушку ошибкой 400
    # на запросах ботов без токена
    return serializers.CharField(required=False, allow_blank=True, write_only=True, default="")


class CallbackRequestCreateSerializer(serializers.ModelSerializer[CallbackRequest]):
    website_url = _honeypot_field()
    captcha_token = _captcha_field()

    class Meta:
        model = CallbackRequest
        fields = ("name", "phone", "preferred_time_window", "website_url", "captcha_token")


class FeedbackRequestCreateSerializer(serializers.ModelSerializer[FeedbackRequest]):
    website_url = _honeypot_field()
    captcha_token = _captcha_field()

    class Meta:
        model = FeedbackRequest
        fields = ("name", "email", "message", "website_url", "captcha_token")


class SubmissionAcceptedSerializer(serializers.Serializer[dict[str, str]]):
    status = serializers.CharField()