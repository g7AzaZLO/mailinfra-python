"""Официальный Python SDK MailInfra.

Быстрый старт::

    from mailinfra import MailInfra

    client = MailInfra()  # ключ из MAILINFRA_API_KEY
    result = client.emails.send(
        from_email="hello@mail.example.ru",
        to="user@example.com",
        subject="Добро пожаловать!",
        html="<p>Привет!</p>",
    )
    print(result.id, result.status)

Что клиент делает за вас:

* ставит `Idempotency-Key` на каждую отправку письма и батча и
  переиспользует его при повторах — сбой сети не превращается в два письма;
* повторяет `429`/`5xx`/таймауты с экспоненциальным backoff и уважает
  `Retry-After`;
* поднимает ошибки API типизированными исключениями
  (`RateLimitError`, `DomainNotVerifiedError`, …);
* валидирует письмо локально по OpenAPI-спеке до сетевого вызова;
* сам обходит страницы списка писем (`client.emails.list_all()`);
* проверяет подписи вебхуков, в том числе в окне ротации секрета
  (`mailinfra.webhooks`).

Покрытие v1 — публичный API писем (`/v1/emails*`). Управление доменами,
шаблонами, вебхуками и ключами — Management API на сессионном токене, оно в
SDK пока не входит.
"""

from __future__ import annotations

from . import webhooks
from ._errors import (
    AllRecipientsSuppressedError,
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    DomainNotAttachedError,
    DomainNotVerifiedError,
    EmailNotScheduledError,
    IdempotencyInFlightError,
    IPNotAllowedError,
    LegalDocumentsNotAcceptedError,
    MailInfraError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ReservedEmailHeaderError,
    SandboxRecipientNotAllowedError,
    SandboxTrialExhaustedError,
    SendAtInvalidError,
    SendAtTooFarError,
    ServerError,
    TemplateRenderError,
    TemplateVariableMissingError,
    UnprocessableError,
)
from ._transport import RetryPolicy
from ._version import __version__
from .client import AsyncMailInfra, MailInfra
from .models import (
    Attachment,
    BatchSendError,
    BatchSendResponseData,
    BatchSendResult,
    BounceCategory,
    BounceType,
    DkimSigningStatus,
    EmailDetail,
    EmailEventPublic,
    EmailEventType,
    EmailListItem,
    EmailListResponse,
    EmailScheduleData,
    EmailSource,
    EmailStatus,
    SendBatchResult,
    SendEmailResult,
    SuppressionReason,
)
from .webhooks import WebhookEvent, WebhookSignatureError

__all__ = [
    "AllRecipientsSuppressedError",
    "APIConnectionError",
    "APITimeoutError",
    "AsyncMailInfra",
    "Attachment",
    "AuthenticationError",
    "BadRequestError",
    "BatchSendError",
    "BatchSendResponseData",
    "BatchSendResult",
    "BounceCategory",
    "BounceType",
    "ConflictError",
    "DkimSigningStatus",
    "DomainNotAttachedError",
    "DomainNotVerifiedError",
    "EmailDetail",
    "EmailEventPublic",
    "EmailEventType",
    "EmailListItem",
    "EmailListResponse",
    "EmailNotScheduledError",
    "EmailScheduleData",
    "EmailSource",
    "EmailStatus",
    "IdempotencyInFlightError",
    "IPNotAllowedError",
    "LegalDocumentsNotAcceptedError",
    "MailInfra",
    "MailInfraError",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitError",
    "ReservedEmailHeaderError",
    "RetryPolicy",
    "SandboxRecipientNotAllowedError",
    "SandboxTrialExhaustedError",
    "SendAtInvalidError",
    "SendAtTooFarError",
    "SendBatchResult",
    "SendEmailResult",
    "ServerError",
    "SuppressionReason",
    "TemplateRenderError",
    "TemplateVariableMissingError",
    "UnprocessableError",
    "WebhookEvent",
    "WebhookSignatureError",
    "__version__",
    "webhooks",
]
