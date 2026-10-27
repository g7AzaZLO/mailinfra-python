# Сгенерировано из sdk/openapi/mailinfra-public.json — НЕ РЕДАКТИРОВАТЬ.
# Перегенерация: bash sdk/scripts/codegen.sh python

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class BatchSendError(BaseModel):
    code: str
    details: dict[str, Any] | None = None
    message: str
    status_code: int


class BounceCategory(str, Enum):
    """
    Нормализованная причина bounce — для UI/Debug Explorer.

    Отдельно от BounceType (hard/soft): категория даёт человекочитаемую
    причину, вычисляется классификатором по enhanced-коду/тексту диагностики.
    См. app.services.bounce_parser.classify_bounce_category.
    """

    invalid_recipient = 'invalid-recipient'
    mailbox_full = 'mailbox-full'
    message_too_large = 'message-too-large'
    content_spam_block = 'content-spam-block'
    policy_rejection = 'policy-rejection'
    reputation_blocklist = 'reputation-blocklist'
    greylisting = 'greylisting'
    dns_mx_failure = 'dns-mx-failure'
    connection_error = 'connection-error'
    rate_limited = 'rate-limited'
    auth_failure = 'auth-failure'
    unknown = 'unknown'


class BounceType(str, Enum):
    hard = 'hard'
    soft = 'soft'


class DkimSigningStatus(str, Enum):
    """
    Достоверность DKIM-подписи письма.

    ``EXPECTED`` — на нашей стороне известно, что домен верифицирован и
    ключ для него существует, поэтому Haraka *должна* была подписать письмо.
    Это не подтверждение факта подписи — Haraka сегодня не сообщает результат
    подписания обратно. ``CONFIRMED``/``FAILED`` зарезервированы под будущий
    callback от Haraka (fast-follow), значения уже есть, миграция не нужна.
    """

    expected = 'expected'
    confirmed = 'confirmed'
    failed = 'failed'
    not_signed = 'not_signed'


class EmailAttachmentInput(BaseModel):
    """
    Вложение в исходящем письме.

    `content_base64` — содержимое файла в base64. Совокупный размер всех
    вложений и пер-файловые лимиты валидируются в
    `app.services.attachments.normalize_attachments`.
    """

    content_base64: Annotated[str, Field(max_length=14348938)]
    content_type: Annotated[str | None, Field(max_length=255)] = (
        'application/octet-stream'
    )
    filename: Annotated[str, Field(max_length=255, min_length=1)]


class EmailEventType(str, Enum):
    queued = 'queued'
    sending = 'sending'
    sent = 'sent'
    delivered = 'delivered'
    smtp_connected = 'smtp_connected'
    opened = 'opened'
    clicked = 'clicked'
    bounced = 'bounced'
    complained = 'complained'
    failed = 'failed'
    simulated = 'simulated'
    deferred = 'deferred'


class EmailSendRequest(BaseModel):
    """
    POST /v1/emails.
    """

    attachments: Annotated[list[EmailAttachmentInput] | None, Field(max_length=10)] = (
        None
    )
    bcc: Annotated[list[EmailStr] | None, Field(max_length=50)] = None
    cc: Annotated[list[EmailStr] | None, Field(max_length=50)] = None
    from_: Annotated[EmailStr, Field(alias='from')]
    from_name: Annotated[str | None, Field(max_length=200)] = None
    headers: dict[str, str] | None = None
    html: Annotated[str | None, Field(max_length=26214400)] = None
    reply_to: Annotated[list[EmailStr] | None, Field(max_length=10)] = None
    subject: Annotated[str | None, Field(max_length=998)] = None
    tags: Annotated[list[str] | None, Field(max_length=10)] = None
    template: Annotated[str | None, Field(max_length=100)] = None
    text: Annotated[str | None, Field(max_length=26214400)] = None
    to: Annotated[list[EmailStr], Field(max_length=50, min_length=1)]
    variables: dict[str, Any] | None = None


class EmailSource(str, Enum):
    """
    Транспорт, через который письмо попало в пайплайн отправки.
    """

    API = 'API'
    SMTP = 'SMTP'
    CAMPAIGN = 'CAMPAIGN'


class EmailStatus(str, Enum):
    QUEUED = 'QUEUED'
    SENDING = 'SENDING'
    SENT = 'SENT'
    DELIVERED = 'DELIVERED'
    BOUNCED = 'BOUNCED'
    COMPLAINED = 'COMPLAINED'
    FAILED = 'FAILED'
    SIMULATED = 'SIMULATED'


class ErrorDetails(BaseModel):
    code: str
    details: dict[str, Any] | None = None
    message: str


class ErrorResponse(BaseModel):
    """
    Единый конверт ошибки. Собирается в ``app.main.error_response``.

    Ровно эта форма отдаётся всеми обработчиками исключений — доменными
    (``MailInfraError``), валидационными и fallback'ом на непойманное
    исключение. Схема подключена к OpenAPI через ``ERROR_RESPONSES``, чтобы
    сгенерированные клиенты знали структуру ошибки, а не только её код.
    """

    error: ErrorDetails
    request_id: str | None = None


class SuppressionReason(str, Enum):
    HARD_BOUNCE = 'HARD_BOUNCE'
    COMPLAINT = 'COMPLAINT'
    UNSUBSCRIBE = 'UNSUBSCRIBE'
    MANUAL = 'MANUAL'


class BatchSendRequest(BaseModel):
    emails: Annotated[list[EmailSendRequest], Field(max_length=100, min_length=1)]


class BatchSendResult(BaseModel):
    error: BatchSendError | None = None
    id: UUID | None = None
    index: int
    skipped_recipients: list[dict[str, str]] | None = None
    status: EmailStatus | None = None


class EmailDetail(BaseModel):
    bcc_emails: list[str] | None = None
    bounce_category: BounceCategory | None = None
    bounce_type: BounceType | None = None
    cc_emails: list[str] | None = None
    created_at: datetime
    delivered_at: datetime | None = None
    dkim_aligned: bool | None = None
    dkim_d: str | None = None
    dkim_selector: str | None = None
    dkim_signing_status: DkimSigningStatus | None = None
    dmarc_aligned: bool | None = None
    failed_at: datetime | None = None
    final_smtp_code: int | None = None
    final_smtp_enhanced: str | None = None
    final_smtp_text: str | None = None
    from_email: str
    from_name: str | None = None
    headers: dict[str, str]
    html_body: str | None = None
    id: UUID
    message_id: str | None = None
    project_id: UUID
    reply_to: list[str] | None = None
    sent_at: datetime | None = None
    source: EmailSource | None = EmailSource.API
    spf_aligned: bool | None = None
    spf_authorized_ip: bool | None = None
    spf_domain: str | None = None
    status: EmailStatus
    subject: str
    suppression_reason: SuppressionReason | None = None
    tags: list[str]
    text_body: str | None = None
    to_emails: list[str]


class EmailEventPublic(BaseModel):
    data: dict[str, Any]
    id: UUID
    ip_address: str | None = None
    timestamp: datetime
    type: EmailEventType
    user_agent: str | None = None


class EmailListItem(BaseModel):
    """
    Сокращённая репрезентация для списка.
    """

    bounce_category: BounceCategory | None = None
    bounce_type: BounceType | None = None
    created_at: datetime
    delivered_at: datetime | None = None
    dkim_aligned: bool | None = None
    dkim_d: str | None = None
    dkim_selector: str | None = None
    dkim_signing_status: DkimSigningStatus | None = None
    dmarc_aligned: bool | None = None
    final_smtp_code: int | None = None
    final_smtp_enhanced: str | None = None
    final_smtp_text: str | None = None
    from_email: str
    id: UUID
    message_id: str | None = None
    project_id: UUID
    sent_at: datetime | None = None
    source: EmailSource | None = EmailSource.API
    spf_aligned: bool | None = None
    spf_authorized_ip: bool | None = None
    spf_domain: str | None = None
    status: EmailStatus
    subject: str
    suppression_reason: SuppressionReason | None = None
    tags: list[str]
    to_emails: list[str]


class EmailSendResponseData(BaseModel):
    id: UUID
    skipped_recipients: list[dict[str, str]] | None = None
    status: EmailStatus


class BatchSendResponseData(BaseModel):
    accepted_count: int
    failed_count: int
    results: list[BatchSendResult]


class DataResponseBatchSendResponseData(BaseModel):
    data: BatchSendResponseData


class DataResponseEmailDetail(BaseModel):
    data: EmailDetail


class DataResponseEmailSendResponseData(BaseModel):
    data: EmailSendResponseData


class DataResponseListEmailEventPublic(BaseModel):
    data: list[EmailEventPublic]


class DataResponseListEmailListItem(BaseModel):
    data: list[EmailListItem]
