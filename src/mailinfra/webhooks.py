"""Проверка подписи вебхуков MailInfra.

MailInfra подписывает каждый POST заголовком::

    X-MailInfra-Signature: t=1715461200,v1=3d4f2e1a…

где `v1` — HMAC-SHA256 от `f"{t}."` + сырое тело запроса, ключ — secret
вебхука (вместе с префиксом `whsec_`). Алгоритм повторяет
`app.services.webhook_service.sign_payload`; вектора подписи общие для
бэкенда и всех SDK — `webhook-signatures.json` (в репозитории SDK — `resources/`).

Во время ротации секрета подписей две — новым секретом и прежним::

    X-MailInfra-Signature: t=1715461200,v1=<новым>,v1=<прежним>

Подпись верна, если совпала любая `v1`, поэтому обработчик с любым из двух
секретов продолжает принимать события, пока прежний не истёк.

Использование (FastAPI)::

    from mailinfra import webhooks

    @app.post("/mailinfra/events")
    async def handle(request: Request) -> dict[str, str]:
        event = webhooks.construct_event(
            body=await request.body(),
            secret=os.environ["MAILINFRA_WEBHOOK_SECRET"],
            signature_header=request.headers["X-MailInfra-Signature"],
        )
        ...
        return {"status": "ok"}

Важно передавать **сырые байты** тела: пересериализованный JSON даст другие
байты и подпись не сойдётся.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

__all__ = [
    "DEFAULT_MAX_AGE_SECONDS",
    "SIGNATURE_HEADER",
    "WebhookEvent",
    "WebhookSignatureError",
    "construct_event",
    "verify",
]

SIGNATURE_HEADER = "X-MailInfra-Signature"
EVENT_ID_HEADER = "X-MailInfra-Event-Id"

# Бэкенд отклоняет подписи старше 5 минут (`SIGNATURE_MAX_AGE_SEC`), держим
# то же окно, чтобы поведение получателя совпадало с ожиданиями отправителя.
DEFAULT_MAX_AGE_SECONDS = 300


class WebhookSignatureError(Exception):
    """Подпись не прошла проверку.

    Attributes:
        reason: Машинная причина: `invalid_secret`, `malformed_header`, `too_old`,
            `signature_mismatch`, `invalid_json`.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class WebhookEvent(BaseModel):
    """Разобранное событие вебхука.

    Attributes:
        id: Идентификатор события: `evt_<uuid>` у событий письма,
            `evt_deliverability_<hex>` у `deliverability.*`, `evt_test_ping`
            у тестовой отправки.
        type: Тип: `email.<событие>` у событий письма, где событие — значение
            `EmailEventType` (например `email.bounced`, `email.scheduled`,
            `email.canceled`), `deliverability.<инцидент>` как есть (например
            `deliverability.dns_degraded`), `test.ping` у тестовой отправки
            из дашборда.
        created_at: Момент события на стороне MailInfra.
        data: Полезная нагрузка. У событий письма `data["email"]` — письмо,
            `data["event"]` — специфика события (MX, IP, bounce-детали, URL
            клика). У `deliverability.*` письма нет: `domain_id`, `domain`,
            `severity`, `title`, `details` инцидента.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    created_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def email(self) -> dict[str, Any] | None:
        """Письмо, к которому относится событие.

        Returns:
            Словарь с `id`, `from`, `to`, `subject`, `tags`, `metadata`,
            `status` или `None` для событий без письма (`deliverability.*`,
            `test.ping`).
        """
        value = self.data.get("email")
        return value if isinstance(value, dict) else None

    @property
    def metadata(self) -> dict[str, str]:
        """`metadata`, переданный при отправке письма.

        Returns:
            Пары «ключ — значение» письма; пустой словарь, если их нет или у
            события нет письма.
        """
        email = self.email
        raw = email.get("metadata") if email else None
        if not isinstance(raw, dict):
            return {}
        return {key: value for key, value in raw.items() if isinstance(value, str)}

    @property
    def email_id(self) -> str | None:
        """Идентификатор письма.

        Returns:
            `id` письма или `None`, если события письма нет.
        """
        email = self.email
        raw = email.get("id") if email else None
        return raw if isinstance(raw, str) else None

    @property
    def is_test(self) -> bool:
        """Тестовое ли это событие («Отправить тест» в дашборде).

        Returns:
            `True` для `test.ping`.
        """
        return self.type == "test.ping"


def sign(*, body: bytes, secret: str, timestamp: int) -> str:
    """Считает hex-подпись тела — та же функция, что на бэкенде.

    Нужна в первую очередь тестам и локальной отладке обработчика: позволяет
    собрать корректный `X-MailInfra-Signature` без реального вебхука.

    Args:
        body: Сырые байты тела запроса.
        secret: Secret вебхука вместе с префиксом `whsec_`.
        timestamp: Unix-время, которое уйдёт в параметр `t`.

    Returns:
        HMAC-SHA256 в hex.
    """
    signing_string = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode("utf-8"), signing_string, hashlib.sha256).hexdigest()


def _any_signature_matches(expected: str, provided: list[str]) -> bool:
    """Сравнивает подпись с каждой `v1` из заголовка за постоянное время.

    Сравниваем в байтах, а не в строках: `hmac.compare_digest` на строке с
    не-ASCII символом бросает `TypeError`, а `provided` целиком приходит из
    заголовка запроса. Мусорный заголовок обязан давать «подпись не сошлась»,
    а не 500 у обработчика вебхука.

    Сравниваются все подписи, без выхода на первом совпадении: время ответа
    не выдаёт, какая из `v1` подошла.

    Args:
        expected: Подпись, посчитанная по телу и секрету.
        provided: Все значения `v1` из заголовка — во время ротации их две.

    Returns:
        `True`, если совпала хотя бы одна.
    """
    expected_bytes = expected.encode("utf-8")
    matches = [hmac.compare_digest(expected_bytes, item.encode("utf-8")) for item in provided]
    return any(matches)


def _require_secret(secret: str) -> None:
    """Отклоняет пустой secret до вычисления подписи.

    Типовой источник — `os.environ.get("MAILINFRA_WEBHOOK_SECRET", "")`: с
    пустым ключом подпись считается, но не совпадает никогда, и обработчик
    молча отклоняет каждую доставку вместо того, чтобы сказать про конфиг.
    Node SDK на пустом ключе падал DOMException'ом; теперь оба отвечают
    `invalid_secret`.

    Args:
        secret: Secret вебхука.

    Raises:
        WebhookSignatureError: Если secret пуст.
    """
    if not secret:
        raise WebhookSignatureError(
            "Пустой secret вебхука: проверьте MAILINFRA_WEBHOOK_SECRET — значение вида "
            "'whsec_…' берётся в дашборде при создании вебхука",
            reason="invalid_secret",
        )


def _parse_header(signature_header: str) -> tuple[int, list[str]]:
    """Разбирает заголовок подписи.

    Все `v1` собираются в список: во время ротации секрета их две, и словарь
    «ключ — значение» молча оставил бы одну — обработчик с другим секретом
    отвергал бы валидные события.

    Args:
        signature_header: Значение `X-MailInfra-Signature`.

    Returns:
        Пара «timestamp, hex-подписи в порядке заголовка».

    Raises:
        WebhookSignatureError: Если заголовок не разбирается.
    """
    timestamp: str | None = None
    signatures: list[str] = []
    try:
        for item in signature_header.split(","):
            # Пробелы вокруг частей допускает и HTTP, и нормализующие прокси:
            # без strip() заголовок `t=…, v1=…` ронял разбор, хотя Node SDK
            # его принимал — один и тот же вебхук доходил не везде.
            key, separator, value = (part.strip() for part in item.partition("="))
            if not separator:
                raise ValueError(f"часть заголовка без '=': {item!r}")
            if key == "t":
                timestamp = value
            elif key == "v1":
                signatures.append(value)
        if timestamp is None or not signatures:
            raise ValueError("нет t или v1")
        return int(timestamp), signatures
    except ValueError as exc:
        raise WebhookSignatureError(
            f"Заголовок {SIGNATURE_HEADER} не разобран: ожидается 't=<unix>,v1=<hex>[,v1=<hex>]'",
            reason="malformed_header",
        ) from exc


def _check_signature(
    *, body: bytes, secret: str, signature_header: str, max_age_seconds: int
) -> None:
    """Проверяет подпись и её возраст, объясняя отказ.

    Порядок проверок важен: пустой secret — ошибка конфига получателя, и
    `reason` обязан указывать на неё раньше, чем на заголовок отправителя.

    Args:
        body: Сырые байты тела запроса.
        secret: Secret вебхука (`whsec_…`).
        signature_header: Значение `X-MailInfra-Signature`.
        max_age_seconds: Допустимый возраст подписи; `0` — не проверять.

    Raises:
        WebhookSignatureError: Secret пуст, заголовок не разобран, подпись
            устарела или ни одна `v1` не совпала.
    """
    _require_secret(secret)
    timestamp, provided = _parse_header(signature_header)

    if max_age_seconds and abs(int(time.time()) - timestamp) > max_age_seconds:
        raise WebhookSignatureError(
            f"Подпись старше {max_age_seconds} с — возможен replay",
            reason="too_old",
        )

    expected = sign(body=body, secret=secret, timestamp=timestamp)
    if not _any_signature_matches(expected, provided):
        raise WebhookSignatureError("Подпись не совпала", reason="signature_mismatch")


def verify(
    *,
    body: bytes,
    secret: str,
    signature_header: str,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> bool:
    """Проверяет подпись, не бросая исключений.

    Args:
        body: Сырые байты тела запроса (до разбора JSON).
        secret: Secret вебхука (`whsec_…`).
        signature_header: Значение `X-MailInfra-Signature`.
        max_age_seconds: Допустимый возраст подписи; защита от replay.
            `0` отключает проверку времени.

    Returns:
        `True`, если подпись валидна (совпала любая из `v1`) и не устарела.
    """
    try:
        _check_signature(
            body=body,
            secret=secret,
            signature_header=signature_header,
            max_age_seconds=max_age_seconds,
        )
    except WebhookSignatureError:
        return False
    return True


def construct_event(
    *,
    body: bytes,
    secret: str,
    signature_header: str,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> WebhookEvent:
    """Проверяет подпись и возвращает типизированное событие.

    Это рекомендуемый способ: тело нельзя разобрать раньше, чем проверена
    подпись, иначе обработчик доверяет неаутентифицированным данным.

    Args:
        body: Сырые байты тела запроса.
        secret: Secret вебхука (`whsec_…`).
        signature_header: Значение `X-MailInfra-Signature`.
        max_age_seconds: Допустимый возраст подписи.

    Returns:
        Разобранное событие.

    Raises:
        WebhookSignatureError: Если secret пуст, ни одна подпись не сошлась,
            подпись устарела, тело не является JSON-объектом или событие
            пришло не по контракту. Причина — в поле `reason`.
    """
    _check_signature(
        body=body,
        secret=secret,
        signature_header=signature_header,
        max_age_seconds=max_age_seconds,
    )

    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise WebhookSignatureError("Тело вебхука не является JSON", reason="invalid_json") from exc

    if not isinstance(payload, dict):
        raise WebhookSignatureError("Тело вебхука не является JSON-объектом", reason="invalid_json")

    try:
        return WebhookEvent.model_validate(payload)
    except PydanticValidationError as exc:
        # Docstring обещает только `WebhookSignatureError`, и обработчик,
        # написанный по документации, ловил ValidationError как 500 — доставка
        # ретраилась бесконечно. Node на том же теле отдавал событие с
        # `type=undefined`; теперь оба отвечают `invalid_payload`.
        raise WebhookSignatureError(
            f"Событие вебхука не по контракту: {exc.errors()[0]['loc']}",
            reason="invalid_payload",
        ) from exc
