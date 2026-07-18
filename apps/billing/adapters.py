# ПОЧЕМУ: источником истины является только прямое обращение к API,
# тело вебхука может быть скомпрометировано

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol
from urllib import error as urlerror
from urllib import request as urlrequest

from pydantic_settings import BaseSettings, SettingsConfigDict

PaymentStatus = Literal["pending", "waiting_for_capture", "succeeded", "canceled"]

_KNOWN_STATUSES: frozenset[str] = frozenset(
    ("pending", "waiting_for_capture", "succeeded", "canceled")
)
_PAYMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9\-]{1,64}$")


class GatewayError(Exception):
    pass


class GatewayNetworkError(GatewayError):
    # ПОЧЕМУ: маркер для SimpleRetryMiddleware (таймауты, 5xx). Ретрай разрешен
    pass


class GatewayContractError(GatewayError):
    # ПОЧЕМУ: фатальная ошибка (битый контракт, 4xx). Ретрай бессмыслен
    pass


class PaymentNotFoundError(GatewayContractError):
    def __init__(self, payment_id: str) -> None:
        super().__init__(f"Платёж {payment_id} не найден в ЮКассе.")
        self.payment_id = payment_id


class InvalidPaymentIdError(GatewayContractError):
    def __init__(self, payment_id: str) -> None:
        super().__init__(f"Недопустимый формат payment_id: {payment_id!r}.")
        self.payment_id = payment_id


@dataclass(frozen=True)
class PaymentInfo:
    id: str
    status: PaymentStatus
    transaction_id: str | None
    amount_kopecks: int
    currency: str


RefundStatus = Literal["pending", "succeeded", "canceled"]

_KNOWN_REFUND_STATUSES: frozenset[str] = frozenset(("pending", "succeeded", "canceled"))


@dataclass(frozen=True)
class RefundInfo:
    id: str
    status: RefundStatus


class PaymentGateway(Protocol):
    def get_payment(self, payment_id: str) -> PaymentInfo: ...

    def create_refund(
        self, payment_id: str, amount_kopecks: int, idempotence_key: str
    ) -> RefundInfo:
        # ПОЧЕМУ: гарантия идемпотентности обеспечивается
        # на стороне ЮКассы по переданному ключу
        ...


class YookassaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="YOOKASSA_")

    shop_id: str
    secret_key: str
    api_base_url: str = "https://api.yookassa.ru/v3"
    timeout_seconds: float = 5.0


class YookassaHttpGateway:
    def __init__(self, settings: YookassaSettings | None = None) -> None:
        # ПОЧЕМУ ignore: shop_id/secret_key заполняет pydantic-settings из env
        self._settings = (
            settings if settings is not None else YookassaSettings()  # type: ignore[call-arg]
        )

    def get_payment(self, payment_id: str) -> PaymentInfo:
        if not _PAYMENT_ID_PATTERN.match(payment_id):
            # ПОЧЕМУ: предотвращает path traversal при подстановке ID
            # из внешнего вебхука в URL
            raise InvalidPaymentIdError(payment_id)

        url = f"{self._settings.api_base_url}/payments/{payment_id}"
        credentials = f"{self._settings.shop_id}:{self._settings.secret_key}"
        token = base64.b64encode(credentials.encode()).decode()
        http_request = urlrequest.Request(
            url, headers={"Authorization": f"Basic {token}"}
        )

        try:
            # FIXME: urllib не поддерживает пулинг TCP-коннектов
            # При росте нагрузки упремся в TIME_WAIT exhaustion
            # Перевести на httpx.Client/requests.Session
            with urlrequest.urlopen(
                http_request, timeout=self._settings.timeout_seconds
            ) as response:
                raw_body: bytes = response.read()
        except urlerror.HTTPError as exc:
            raise _classify_http_error(payment_id, exc) from exc
        except urlerror.URLError as exc:
            raise GatewayNetworkError(
                f"Сбой соединения с ЮКассой: {exc.reason}."
            ) from exc

        return _parse_payment_body(payment_id, raw_body)

    def create_refund(
        self, payment_id: str, amount_kopecks: int, idempotence_key: str
    ) -> RefundInfo:
        if not _PAYMENT_ID_PATTERN.match(payment_id):
            raise InvalidPaymentIdError(payment_id)

        url = f"{self._settings.api_base_url}/refunds"
        credentials = f"{self._settings.shop_id}:{self._settings.secret_key}"
        token = base64.b64encode(credentials.encode()).decode()
        body = json.dumps(
            {
                "payment_id": payment_id,
                "amount": {
                    "value": _kopecks_to_value(amount_kopecks),
                    "currency": "RUB",
                },
            }
        ).encode()
        http_request = urlrequest.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Basic {token}",
                "Content-Type": "application/json",
                "Idempotence-Key": idempotence_key,
            },
        )

        try:
            # FIXME: urllib не поддерживает пулинг TCP-коннектов
            # При росте нагрузки упремся в TIME_WAIT exhaustion
            # Перевести на httpx.Client/requests.Session
            with urlrequest.urlopen(
                http_request, timeout=self._settings.timeout_seconds
            ) as response:
                raw_body: bytes = response.read()
        except urlerror.HTTPError as exc:
            raise _classify_http_error(payment_id, exc) from exc
        except urlerror.URLError as exc:
            raise GatewayNetworkError(
                f"Сбой соединения с ЮКассой: {exc.reason}."
            ) from exc

        return _parse_refund_body(payment_id, raw_body)


def _parse_payment_body(payment_id: str, raw_body: bytes) -> PaymentInfo:
    try:
        data = json.loads(raw_body)
    except ValueError as exc:
        raise GatewayContractError(
            f"Битый JSON в ответе по платежу {payment_id}."
        ) from exc

    if not isinstance(data, dict):
        raise GatewayContractError(f"Неожиданная форма ответа по платежу {payment_id}.")

    status = data.get("status")
    if not isinstance(status, str) or status not in _KNOWN_STATUSES:
        raise GatewayContractError(
            f"Неизвестный статус платежа {payment_id}: {status!r}."
        )

    amount_kopecks, currency = _parse_amount(payment_id, data.get("amount"))

    metadata = data.get("metadata")
    transaction_id: str | None = None
    if isinstance(metadata, dict):
        raw_transaction_id = metadata.get("transaction_id")
        if isinstance(raw_transaction_id, str):
            transaction_id = raw_transaction_id

    verified_status: PaymentStatus = status  # type: ignore[assignment]  # сужение проверено членством в _KNOWN_STATUSES
    return PaymentInfo(
        id=payment_id,
        status=verified_status,
        transaction_id=transaction_id,
        amount_kopecks=amount_kopecks,
        currency=currency,
    )


def _parse_amount(payment_id: str, raw_amount: object) -> tuple[int, str]:
    # ПОЧЕМУ: транзит денег через float запрещен из-за потери точности,
    # парсим строго через Decimal
    if not isinstance(raw_amount, dict):
        raise GatewayContractError(
            f"В ответе по платежу {payment_id} нет объекта amount."
        )

    value = raw_amount.get("value")
    currency = raw_amount.get("currency")
    if not isinstance(value, str) or not isinstance(currency, str):
        raise GatewayContractError(
            f"Платёж {payment_id}: amount.value/currency не строки."
        )

    try:
        kopecks = Decimal(value) * 100
    except InvalidOperation as exc:
        raise GatewayContractError(
            f"Платёж {payment_id}: нечисловая сумма {value!r}."
        ) from exc

    if kopecks != kopecks.to_integral_value() or kopecks < 0:
        raise GatewayContractError(
            f"Платёж {payment_id}: недопустимая сумма {value!r}."
        )

    return int(kopecks), currency


def _classify_http_error(payment_id: str, exc: urlerror.HTTPError) -> GatewayError:
    if exc.code == 404:
        return PaymentNotFoundError(payment_id)
    if exc.code >= 500:
        return GatewayNetworkError(f"ЮКасса ответила HTTP {exc.code}.")
    return GatewayContractError(f"ЮКасса отвергла запрос: HTTP {exc.code}.")


def _kopecks_to_value(amount_kopecks: int) -> str:
    return f"{amount_kopecks // 100}.{amount_kopecks % 100:02d}"


def _parse_refund_body(payment_id: str, raw_body: bytes) -> RefundInfo:
    try:
        data = json.loads(raw_body)
    except ValueError as exc:
        raise GatewayContractError(
            f"Битый JSON в ответе на возврат по платежу {payment_id}."
        ) from exc

    if not isinstance(data, dict):
        raise GatewayContractError(
            f"Неожиданная форма ответа на возврат по платежу {payment_id}."
        )

    refund_id = data.get("id")
    status = data.get("status")
    if not isinstance(refund_id, str) or not refund_id:
        raise GatewayContractError(f"Возврат по платежу {payment_id}: нет id.")
    if not isinstance(status, str) or status not in _KNOWN_REFUND_STATUSES:
        raise GatewayContractError(
            f"Возврат по платежу {payment_id}: неизвестный статус {status!r}."
        )

    verified_status: RefundStatus = status  # type: ignore[assignment]  # сужение проверено членством
    return RefundInfo(id=refund_id, status=verified_status)
