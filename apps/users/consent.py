from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from rest_framework import serializers
from rest_framework.request import Request

from apps.core.net import client_ip
from apps.users.models import ConsentPurpose, Parent, PersonalDataConsent

_USER_AGENT_MAX_LENGTH = 512


@dataclass(frozen=True, slots=True)
class ConsentSource:
    """Откуда пришло согласие — снимок запроса для журнала-доказательства."""

    ip: str | None
    user_agent: str

    @classmethod
    def from_request(cls, request: HttpRequest | Request) -> ConsentSource:
        raw_ip = client_ip(request)
        try:
            ip: str | None = str(ip_address(raw_ip))
        except ValueError:
            # ПОЧЕМУ: мусор в заголовке не должен ронять заявку клиента —
            # согласие фиксируем и без IP
            ip = None
        user_agent: str = request.META.get("HTTP_USER_AGENT", "")
        return cls(ip=ip, user_agent=user_agent[:_USER_AGENT_MAX_LENGTH])


def pd_consent_field() -> serializers.BooleanField:
    # ПОЧЕМУ: галочка на фронте не может стоять заранее (152-ФЗ: согласие
    # «конкретное и сознательное») — бэкенд требует явное true
    return serializers.BooleanField(
        write_only=True,
        help_text=(
            "Согласие на обработку персональных данных. Обязательно true; "
            "галочка на фронте не должна стоять по умолчанию"
        ),
    )


def require_true(value: bool) -> bool:
    if value is not True:
        raise serializers.ValidationError(
            "Без согласия на обработку персональных данных отправить нельзя."
        )
    return value


def record_consent(
    purpose: ConsentPurpose,
    source: ConsentSource,
    *,
    parent: Parent | None = None,
    email: str = "",
    phone: str = "",
    source_id: int | None = None,
) -> PersonalDataConsent:
    # ПОЧЕМУ: вызывается в той же транзакции, что и запись заявки: нет
    # заявки без согласия и согласия на несохранённую заявку
    return PersonalDataConsent.objects.create(
        purpose=purpose,
        document_version=settings.PD_CONSENT_VERSION,
        parent=parent,
        email=email,
        phone=phone,
        source_id=source_id,
        ip=source.ip,
        user_agent=source.user_agent,
    )


def grant_registration_consent(parent: Parent, source: ConsentSource) -> None:
    """Фиксирует согласие родителя из анкеты. Повторная галочка — без дублей."""
    now = timezone.now()
    with transaction.atomic():
        # ПОЧЕМУ: условный UPDATE вместо чтения-проверки-записи — два
        # параллельных PATCH (дабл-клик) не создадут две записи журнала
        granted = Parent.objects.filter(
            pk=parent.pk, pd_consent_at__isnull=True
        ).update(pd_consent_at=now, updated_at=now)
        if not granted:
            return
        record_consent(
            ConsentPurpose.REGISTRATION,
            source,
            parent=parent,
            email=parent.email,
            phone=str(parent.phone or ""),
        )
    parent.pd_consent_at = now
