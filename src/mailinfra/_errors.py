"""Типизированные исключения поверх конверта ошибок MailInfra.

API отдаёт любую ошибку одной формой (см. https://docs.mailinfra.ru/errors)::

    {"error": {"code": "domain_not_verified", "message": "…", "details": {}},
     "request_id": "3d6f0dce…"}

`code` — стабильный машинный код, его и надо ветвить в коде. Классы ниже
подобраны так, чтобы ловить можно было на трёх уровнях: всё
(`MailInfraError`), категория HTTP-статуса (`NotFoundError`) или конкретная
причина (`DomainNotVerifiedError`). Неизвестный код деградирует до класса по
статусу: новый код на бэкенде не роняет старый SDK.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AllRecipientsSuppressedError",
    "APIConnectionError",
    "APITimeoutError",
    "AuthenticationError",
    "BadRequestError",
    "ConflictError",
    "DomainNotAttachedError",
    "DomainNotVerifiedError",
    "IdempotencyInFlightError",
    "LegalDocumentsNotAcceptedError",
    "MailInfraError",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitError",
    "ReservedEmailHeaderError",
    "SandboxRecipientNotAllowedError",
    "SandboxTrialExhaustedError",
    "ServerError",
    "TemplateRenderError",
    "TemplateVariableMissingError",
    "UnprocessableError",
    "error_from_response",
]


class MailInfraError(Exception):
    """Базовая ошибка SDK. Ловите её, если детали не важны.

    Attributes:
        message: Человеко-читаемое сообщение от API (на русском).
        status_code: HTTP-статус ответа; `None` для сетевых ошибок.
        code: Машинный код ошибки (`error.code`).
        details: Безопасные структурированные детали (`error.details`).
        request_id: Идентификатор запроса для поддержки (`request_id`,
            он же заголовок `X-Request-ID`).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str = "unknown_error",
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.details: dict[str, Any] = details or {}
        self.request_id = request_id

    def __str__(self) -> str:
        parts = [self.message]
        if self.code and self.code != "unknown_error":
            parts.append(f"code={self.code}")
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return " ".join(parts) if len(parts) == 1 else f"{parts[0]} ({', '.join(parts[1:])})"


# ---------- Транспортные ошибки (ответа от API не было) ----------


class APIConnectionError(MailInfraError):
    """Не удалось получить ответ: DNS, TCP, TLS, обрыв соединения."""

    def __init__(self, message: str = "Не удалось соединиться с API MailInfra") -> None:
        super().__init__(message, code="api_connection_error")


class APITimeoutError(APIConnectionError):
    """Запрос не успел уложиться в таймаут."""

    def __init__(self, message: str = "Истёк таймаут запроса к API MailInfra") -> None:
        MailInfraError.__init__(self, message, code="api_timeout")


# ---------- Ошибки по HTTP-статусу ----------


class BadRequestError(MailInfraError):
    """`400` — запрос некорректен."""


class AuthenticationError(MailInfraError):
    """`401` — API-ключ отсутствует, невалиден или отозван."""


class PermissionDeniedError(MailInfraError):
    """`403` — у ключа нет прав на операцию.

    Типичная причина: ключ выпущен с правами `READ_ONLY` (тогда отправка
    запрещена) или `SENDING_ONLY` (тогда запрещено чтение).
    """


class NotFoundError(MailInfraError):
    """`404` — ресурс не найден или принадлежит другому проекту."""


class ConflictError(MailInfraError):
    """`409` — конфликт состояния."""


class UnprocessableError(MailInfraError):
    """`422` — не прошла валидация полей.

    Attributes:
        fields: Список `{"field", "message", "code"}` из `details.fields`,
            если бэкенд его прислал (код `request_validation_failed`).
    """

    @property
    def fields(self) -> list[dict[str, Any]]:
        """Пер-полевые причины отказа.

        Returns:
            Список описаний полей; пустой, если детализации нет.
        """
        raw = self.details.get("fields")
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


class RateLimitError(MailInfraError):
    """`429` — исчерпан лимит отправки: месячный объём или дневной потолок.

    Attributes:
        retry_after: Секунды до безопасного повтора из заголовка
            `Retry-After`; `None`, если заголовка не было.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = 429,
        code: str = "rate_limit_exceeded",
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            code=code,
            details=details,
            request_id=request_id,
        )
        self.retry_after = retry_after


class ServerError(MailInfraError):
    """`5xx` — ошибка на стороне MailInfra. Такие запросы SDK ретраит сам."""


# ---------- Ошибки по конкретному `error.code` ----------
#
# Базовый класс каждой — класс того HTTP-статуса, с которым бэкенд реально
# отдаёт код (см. `app.services.email_service`): `except UnprocessableError`
# обязан ловить и `DomainNotVerifiedError`, иначе документированный приём
# «ловить по категории» молча пропускает половину ошибок отправки.


class DomainNotVerifiedError(UnprocessableError):
    """`422` — домен отправителя ещё не подтверждён (DNS-записи не сошлись)."""


class DomainNotAttachedError(UnprocessableError):
    """`422` — домен не привязан к проекту, которому принадлежит ключ."""


class AllRecipientsSuppressedError(UnprocessableError):
    """`422` — все получатели в списке подавления, отправлять некому."""


class SandboxRecipientNotAllowedError(UnprocessableError):
    """`422` — пробное письмо с sandbox-домена (LIVE-ключ) уходит только владельцу ключа."""


class SandboxTrialExhaustedError(PermissionDeniedError):
    """`403` — пробная sandbox-отправка уже израсходована; нужен свой домен."""


class TemplateRenderError(UnprocessableError):
    """`422` — шаблон не отрендерился."""


class TemplateVariableMissingError(UnprocessableError):
    """`422` — в `variables` не хватает переменной, которую требует шаблон."""


class ReservedEmailHeaderError(UnprocessableError):
    """`422` — попытка переопределить системный заголовок письма."""


class IdempotencyInFlightError(ConflictError):
    """`409` — запрос с таким `Idempotency-Key` ещё выполняется, повторите позже."""


class LegalDocumentsNotAcceptedError(UnprocessableError):
    """`422` — не приняты обязательные юридические документы.

    Политика, оферта или DPA ждут согласия владельца организации, до этого
    отправка заблокирована. В `details["pending"]` — список документов,
    которые нужно принять.
    """


_STATUS_ERRORS: dict[int, type[MailInfraError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: UnprocessableError,
    429: RateLimitError,
}

# Коды, которые бэкенд кладёт в `details.code` при отказе отправки.
# Источник истины — места `raise` в `app.services.{email_service,
# template_service, idempotency, rate_limit}`: только они говорят, какой код
# реально выбрасывается и с каким статусом.
_CODE_ERRORS: dict[str, type[MailInfraError]] = {
    "all_recipients_suppressed": AllRecipientsSuppressedError,
    "domain_not_attached": DomainNotAttachedError,
    "domain_not_verified": DomainNotVerifiedError,
    "idempotency_in_flight": IdempotencyInFlightError,
    "legal_documents_not_accepted": LegalDocumentsNotAcceptedError,
    "reserved_email_header": ReservedEmailHeaderError,
    "rps_exceeded": RateLimitError,
    "sandbox_recipient_not_allowed": SandboxRecipientNotAllowedError,
    "sandbox_trial_exhausted": SandboxTrialExhaustedError,
    "template_render_error": TemplateRenderError,
    "template_variable_missing": TemplateVariableMissingError,
}


def _status_error_class(status_code: int) -> type[MailInfraError]:
    """Возвращает класс ошибки по HTTP-статусу.

    Args:
        status_code: HTTP-статус ответа.

    Returns:
        Класс исключения; `ServerError` для `5xx`, `MailInfraError` для
        неожиданных статусов.
    """
    if status_code in _STATUS_ERRORS:
        return _STATUS_ERRORS[status_code]
    if status_code >= 500:
        return ServerError
    return MailInfraError


def error_from_response(
    *,
    status_code: int,
    body: Any,
    request_id: str | None,
    retry_after: int | None = None,
) -> MailInfraError:
    """Собирает исключение из ответа API.

    Устойчиво к не-JSON телу и к ответу не по контракту: в этом случае берётся
    класс по статусу и общее сообщение — сеть и прокси умеют возвращать HTML.

    Args:
        status_code: HTTP-статус ответа.
        body: Разобранное тело ответа (обычно `dict`) или сырая строка.
        request_id: Значение заголовка `X-Request-ID`, если есть.
        retry_after: Разобранный `Retry-After` в секундах, если есть.

    Returns:
        Экземпляр подходящего подкласса `MailInfraError`.
    """
    code = "unknown_error"
    message = f"Запрос завершился со статусом {status_code}"
    details: dict[str, Any] = {}

    if isinstance(body, dict):
        envelope = body.get("error")
        if isinstance(envelope, dict):
            raw_code = envelope.get("code")
            raw_message = envelope.get("message")
            raw_details = envelope.get("details")
            if isinstance(raw_code, str) and raw_code:
                code = raw_code
            if isinstance(raw_message, str) and raw_message:
                message = raw_message
            if isinstance(raw_details, dict):
                details = raw_details
        raw_request_id = body.get("request_id")
        if request_id is None and isinstance(raw_request_id, str):
            request_id = raw_request_id

    # `details.code` уточняет причину внутри статуса (например, 429 →
    # `rps_exceeded`), поэтому он приоритетнее верхнеуровневого кода.
    detail_code = details.get("code")
    if isinstance(detail_code, str) and detail_code in _CODE_ERRORS:
        code = detail_code

    error_class = _CODE_ERRORS.get(code) or _status_error_class(status_code)

    if issubclass(error_class, RateLimitError):
        return error_class(
            message,
            status_code=status_code,
            code=code,
            details=details,
            request_id=request_id,
            retry_after=retry_after,
        )

    return error_class(
        message,
        status_code=status_code,
        code=code,
        details=details,
        request_id=request_id,
    )
