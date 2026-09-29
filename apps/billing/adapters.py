# ПОЧЕМУ: источником истины является только прямое обращение к API,
# тело вебхука может быть скомпрометировано

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol

import httpx
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


@dataclass(frozen=True)
class CreatedPayment:
    id: str
    status: PaymentStatus
    confirmation_url: str


class PaymentGateway(Protocol):
    def get_payment(self, payment_id: str) -> PaymentInfo: ...

    def create_payment(
        self,
        *,
        amount_kopecks: int,
        transaction_id: str,
        idempotence_key: str,
        description: str,
    ) -> CreatedPayment:
        # ПОЧЕМУ: metadata.transaction_id — единственная связь платежа
        # провайдера с нашей транзакцией при верификации вебхука
        ...

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
    return_url: str = "http://localhost:3000/checkout/result"


_pooled_client: httpx.Client | None = None
_pooled_client_lock = threading.Lock()


def _shared_http_client() -> httpx.Client:
    # ПОЧЕМУ: один клиент на процесс — keep-alive и пул TCP/TLS-соединений
    # к api.yookassa.ru живут между вызовами и тиками крона, а не открываются
    # на каждый запрос (TIME_WAIT exhaustion). httpx.Client потокобезопасен.
    # Создаётся лениво — уже после fork воркера, сокеты не делятся между процессами
    global _pooled_client
    if _pooled_client is None:
        with _pooled_client_lock:
            if _pooled_client is None:
                _pooled_client = httpx.Client()
    return _pooled_client


class YookassaHttpGateway:
    def __init__(
        self,
        settings: YookassaSettings | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # ПОЧЕМУ ignore: shop_id/secret_key заполняет pydantic-settings из env
        self._settings = (
            settings if settings is not None else YookassaSettings()  # type: ignore[call-arg]
        )
        # ПОЧЕМУ transport: единственная легальная точка подмены HTTP в тестах
        # (httpx.MockTransport) — мокается сетевая граница, а не наш код.
        # С подменой клиент свой, чтобы не отравить общий пул процесса
        self._client = (
            httpx.Client(transport=transport) if transport is not None else None
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        action: str,
        json_body: object = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        # ПОЧЕМУ: общая классификация транспорта — сеть и 5xx ретраятся
        # (GatewayNetworkError), 4xx разбирает вызывающий: их смысл зависит
        # от операции (404 платежа — поддельный вебхук, а не сбой)
        client = self._client if self._client is not None else _shared_http_client()
        try:
            response = client.request(
                method,
                f"{self._settings.api_base_url}{path}",
                json=json_body,
                headers=headers,
                auth=(self._settings.shop_id, self._settings.secret_key),
                timeout=self._settings.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise GatewayNetworkError(
                f"Сбой соединения с ЮКассой ({action}): {exc}."
            ) from exc
        if response.status_code >= 500:
            raise GatewayNetworkError(
                f"ЮКасса ответила HTTP {response.status_code} ({action})."
            )
        return response

    def create_payment(
        self,
        *,
        amount_kopecks: int,
        transaction_id: str,
        idempotence_key: str,
        description: str,
    ) -> CreatedPayment:
        body = {
            "amount": {
                "value": _kopecks_to_value(amount_kopecks),
                "currency": "RUB",
            },
            "capture": True,
            "confirmation": {
                "type": "redirect",
                "return_url": self._settings.return_url,
            },
            "description": description,
            "metadata": {"transaction_id": transaction_id},
        }
        response = self._request(
            "POST",
            "/payments",
            action="создание платежа",
            json_body=body,
            headers={"Idempotence-Key": idempotence_key},
        )
        if response.status_code >= 400:
            raise GatewayContractError(
                f"ЮКасса отвергла создание платежа: HTTP {response.status_code}."
            )
        return _parse_created_payment_body(transaction_id, response.content)

    def get_payment(self, payment_id: str) -> PaymentInfo:
        if not _PAYMENT_ID_PATTERN.match(payment_id):
            # ПОЧЕМУ: предотвращает path traversal при подстановке ID
            # из внешнего вебхука в URL
            raise InvalidPaymentIdError(payment_id)

        response = self._request(
            "GET", f"/payments/{payment_id}", action=f"запрос платежа {payment_id}"
        )
        _raise_for_client_error(payment_id, response)
        return _parse_payment_body(payment_id, response.content)

    def create_refund(
        self, payment_id: str, amount_kopecks: int, idempotence_key: str
    ) -> RefundInfo:
        if not _PAYMENT_ID_PATTERN.match(payment_id):
            raise InvalidPaymentIdError(payment_id)

        response = self._request(
            "POST",
            "/refunds",
            action=f"возврат по платежу {payment_id}",
            json_body={
                "payment_id": payment_id,
                "amount": {
                    "value": _kopecks_to_value(amount_kopecks),
                    "currency": "RUB",
                },
            },
            headers={"Idempotence-Key": idempotence_key},
        )
        _raise_for_client_error(payment_id, response)
        return _parse_refund_body(payment_id, response.content)


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


def _raise_for_client_error(payment_id: str, response: httpx.Response) -> None:
    if response.status_code == 404:
        raise PaymentNotFoundError(payment_id)
    if response.status_code >= 400:
        raise GatewayContractError(
            f"ЮКасса отвергла запрос: HTTP {response.status_code}."
        )


def _kopecks_to_value(amount_kopecks: int) -> str:
    return f"{amount_kopecks // 100}.{amount_kopecks % 100:02d}"


def _parse_created_payment_body(transaction_id: str, raw_body: bytes) -> CreatedPayment:
    try:
        data = json.loads(raw_body)
    except ValueError as exc:
        raise GatewayContractError(
            f"Битый JSON в ответе на создание платежа (транзакция {transaction_id})."
        ) from exc

    if not isinstance(data, dict):
        raise GatewayContractError(
            f"Неожиданная форма ответа на создание платежа "
            f"(транзакция {transaction_id})."
        )

    payment_id = data.get("id")
    status = data.get("status")
    if not isinstance(payment_id, str) or not payment_id:
        raise GatewayContractError(
            f"Создание платежа (транзакция {transaction_id}): нет id."
        )
    if not isinstance(status, str) or status not in _KNOWN_STATUSES:
        raise GatewayContractError(
            f"Создание платежа {payment_id}: неизвестный статус {status!r}."
        )

    confirmation = data.get("confirmation")
    confirmation_url = (
        confirmation.get("confirmation_url") if isinstance(confirmation, dict) else None
    )
    if not isinstance(confirmation_url, str) or not confirmation_url:
        raise GatewayContractError(
            f"Создание платежа {payment_id}: нет confirmation_url."
        )

    verified_status: PaymentStatus = status  # type: ignore[assignment]  # сужение проверено членством
    return CreatedPayment(
        id=payment_id,
        status=verified_status,
        confirmation_url=confirmation_url,
    )


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
