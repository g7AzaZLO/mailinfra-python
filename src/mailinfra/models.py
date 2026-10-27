"""Публичные модели SDK.

Тело моделей сгенерировано из `https://docs.mailinfra.ru/openapi/mailinfra-public.json`
(`mailinfra._generated.models`) — править его руками нельзя, перегенерация
затрёт. Здесь только публичные имена и то, что генератор выразить не может:
результат отправки с флагом идемпотентного повтора и удобный тип вложения.

Соответствие моделей спеке проверяется автоматически
(`tests/test_spec_parity.py`), поэтому реэкспорт не может незаметно разойтись
с API.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ._generated.models import (
    BatchSendError,
    BatchSendRequest,
    BatchSendResponseData,
    BatchSendResult,
    BounceCategory,
    BounceType,
    DkimSigningStatus,
    EmailAttachmentInput,
    EmailDetail,
    EmailEventPublic,
    EmailEventType,
    EmailListItem,
    EmailSendRequest,
    EmailSendResponseData,
    EmailSource,
    EmailStatus,
    ErrorDetails,
    ErrorResponse,
    SuppressionReason,
)

__all__ = [
    "Attachment",
    "BatchSendError",
    "BatchSendRequest",
    "BatchSendResponseData",
    "BatchSendResult",
    "BounceCategory",
    "BounceType",
    "DkimSigningStatus",
    "EmailAttachmentInput",
    "EmailDetail",
    "EmailEventPublic",
    "EmailEventType",
    "EmailListItem",
    "EmailSendRequest",
    "EmailSendResponseData",
    "EmailSource",
    "EmailStatus",
    "ErrorDetails",
    "ErrorResponse",
    "SendEmailResult",
    "SuppressionReason",
]


@dataclass(frozen=True)
class Attachment:
    """Вложение, которое SDK сам закодирует в base64.

    В API вложение уходит как base64-строка (`EmailAttachmentInput`), но
    вызывающему удобнее отдать байты файла. Если base64 уже готов —
    передавайте `content_base64`.

    Attributes:
        filename: Имя файла в письме.
        content: Сырые байты файла.
        content_base64: Готовая base64-строка (альтернатива `content`).
        content_type: MIME-тип; по умолчанию `application/octet-stream`.
    """

    filename: str
    content: bytes | None = None
    content_base64: str | None = None
    content_type: str = "application/octet-stream"

    def __post_init__(self) -> None:
        if (self.content is None) == (self.content_base64 is None):
            raise ValueError("Укажите ровно одно из content / content_base64")


class SendEmailResult(BaseModel):
    """Результат отправки одного письма.

    Attributes:
        id: Идентификатор письма в MailInfra.
        status: Статус сразу после приёма (`QUEUED`, либо `SIMULATED` для
            sandbox-ключа `mi_test_…`).
        skipped_recipients: Получатели, которых API отбросил (например,
            адрес в списке подавления) с причиной в значении.
        replayed: `True`, если это идемпотентный повтор: письмо было принято
            предыдущим запросом с тем же `Idempotency-Key`, повторной
            отправки не произошло (ответ `200` + `Idempotent-Replayed`).
        idempotency_key: Ключ, с которым ушёл запрос — сгенерированный SDK
            или переданный вызывающим. Стоит залогировать: с ним повтор
            гарантированно не создаст второе письмо.
        request_id: Идентификатор запроса для обращений в поддержку.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    status: EmailStatus
    skipped_recipients: list[dict[str, str]] = Field(default_factory=list)
    replayed: bool = False
    idempotency_key: str | None = None
    request_id: str | None = None

    @classmethod
    def from_response(
        cls,
        data: EmailSendResponseData,
        *,
        replayed: bool,
        idempotency_key: str | None,
        request_id: str | None,
    ) -> SendEmailResult:
        """Собирает результат из данных ответа и метаданных запроса.

        Args:
            data: Разобранное поле `data` ответа API.
            replayed: Был ли ответ идемпотентным повтором.
            idempotency_key: Ключ идемпотентности запроса.
            request_id: Значение заголовка `X-Request-ID`.

        Returns:
            Неизменяемый результат отправки.
        """
        return cls(
            id=data.id,
            status=data.status,
            skipped_recipients=list(data.skipped_recipients or []),
            replayed=replayed,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
