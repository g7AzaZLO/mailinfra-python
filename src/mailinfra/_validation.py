"""Клиентская валидация запросов сверх того, что выражает OpenAPI-спека.

Сгенерированные модели проверяют то, что записано в спеке машинно: длины строк,
число элементов, диапазон `ttl`. Часть лимитов API живёт только в описании
полей, и генератор их не видит:

* `metadata` — не больше 20 ключей; ключ — 1–40 символов `[A-Za-z0-9_.-]`;
  значение — строка до 500 символов без NUL; весь объект — до 4096 байт JSON
  в UTF-8 (источник истины — `app.services.email_metadata.normalize_metadata`);
* `send_at` — только с часовым поясом (`AwareDatetime` на бэкенде): у наивного
  времени нет однозначного момента, и письмо ушло бы на часовой пояс раньше
  или позже, чем задумано.

Проверки добавлены валидаторами в подклассы сгенерированных моделей, поэтому
нарушения приходят тем же списком `details["fields"]`, что и лимиты спеки, —
одной ошибкой на все поля, а не по одному полю за вызов.

Горизонт `send_at` (7 дней) здесь сознательно не проверяется: он считается от
момента приёма по часам сервера, а при переносе — от приёма исходного письма,
которого клиент не знает. Об этом честно скажет сервер (`SendAtTooFarError`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator
from pydantic import ValidationError as PydanticValidationError

from ._errors import MailInfraError
from .models import EmailRescheduleRequest, EmailSendRequest

__all__ = [
    "MAX_METADATA_BYTES",
    "MAX_METADATA_KEYS",
    "MAX_METADATA_KEY_LENGTH",
    "MAX_METADATA_VALUE_LENGTH",
    "RescheduleRequest",
    "SendRequest",
    "check_metadata",
    "dump_validated",
    "field_errors",
    "require_timezone",
]

MAX_METADATA_KEYS = 20
MAX_METADATA_KEY_LENGTH = 40
MAX_METADATA_VALUE_LENGTH = 500
MAX_METADATA_BYTES = 4096

_METADATA_KEY_RE = re.compile(rf"[A-Za-z0-9_.\-]{{1,{MAX_METADATA_KEY_LENGTH}}}")


def check_metadata(metadata: dict[str, str]) -> dict[str, str]:
    """Проверяет лимиты `metadata`, которых нет в спеке.

    Args:
        metadata: Пары «ключ — значение» письма; типы pydantic уже проверил.

    Returns:
        То же значение без изменений.

    Raises:
        ValueError: Нарушен лимит. Pydantic превращает это в ошибку поля
            `metadata`, а `dump_validated` — в `MailInfraError`.
    """
    if len(metadata) > MAX_METADATA_KEYS:
        raise ValueError(f"не больше {MAX_METADATA_KEYS} ключей, передано {len(metadata)}")
    for key, value in metadata.items():
        if not _METADATA_KEY_RE.fullmatch(key):
            raise ValueError(
                f"ключ {key!r}: 1–{MAX_METADATA_KEY_LENGTH} символов — латиница, цифры, "
                "'_', '-', '.'"
            )
        if len(value) > MAX_METADATA_VALUE_LENGTH:
            raise ValueError(f"значение {key!r} длиннее {MAX_METADATA_VALUE_LENGTH} символов")
        if "\x00" in value:
            raise ValueError(f"значение {key!r} содержит символ NUL")
    # Размер считается ровно как на бэкенде: `json.dumps` с разделителями по
    # умолчанию (", " и ": "). Компактный JSON короче, и объект у самой границы
    # прошёл бы клиентскую проверку, чтобы получить 422 от API.
    size = len(json.dumps(metadata, ensure_ascii=False).encode("utf-8"))
    if size > MAX_METADATA_BYTES:
        raise ValueError(f"в JSON занимает {size} байт, допустимо {MAX_METADATA_BYTES}")
    return metadata


def require_timezone(moment: datetime) -> datetime:
    """Отклоняет время без часового пояса.

    Args:
        moment: Время отправки, уже разобранное pydantic из `datetime` или
            ISO 8601 строки.

    Returns:
        То же время без изменений.

    Raises:
        ValueError: У времени нет смещения от UTC.
    """
    if moment.utcoffset() is None:
        raise ValueError(
            "нужен часовой пояс: datetime с tzinfo (например, tzinfo=timezone.utc) "
            "или строка со смещением, например 2026-11-01T09:00:00+03:00"
        )
    return moment


class SendRequest(EmailSendRequest):
    """`EmailSendRequest` с лимитами `metadata` и `send_at`, которых нет в спеке."""

    @field_validator("metadata")
    @classmethod
    def metadata_limits(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        """Проверяет лимиты `metadata`.

        Args:
            value: `metadata` письма.

        Returns:
            То же значение.
        """
        return None if value is None else check_metadata(value)

    @field_validator("send_at")
    @classmethod
    def send_at_has_timezone(cls, value: datetime | None) -> datetime | None:
        """Требует часовой пояс у `send_at`.

        Args:
            value: Время отложенной отправки.

        Returns:
            То же значение.
        """
        return None if value is None else require_timezone(value)


class RescheduleRequest(EmailRescheduleRequest):
    """`EmailRescheduleRequest` с обязательным часовым поясом у `send_at`."""

    @field_validator("send_at")
    @classmethod
    def send_at_has_timezone(cls, value: datetime) -> datetime:
        """Требует часовой пояс у нового `send_at`.

        Args:
            value: Новое время отправки.

        Returns:
            То же значение.
        """
        return require_timezone(value)


def field_errors(exc: PydanticValidationError) -> tuple[list[dict[str, str]], str]:
    """Раскладывает ошибку pydantic по полям.

    Args:
        exc: Ошибка валидации.

    Returns:
        Пара «список `{"field", "message"}` для `details["fields"]`, сводка
        для текста исключения».
    """
    fields = [
        {"field": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
        for error in exc.errors()
    ]
    summary = "; ".join(f"{item['field']}: {item['message']}" for item in fields)
    return fields, summary


def dump_validated(
    model: type[BaseModel], payload: Mapping[str, Any], *, failure_message: str
) -> dict[str, Any]:
    """Проверяет тело запроса моделью и отдаёт JSON для API.

    Args:
        model: Модель запроса (`SendRequest`, `RescheduleRequest`).
        payload: Тело запроса без пустых значений.
        failure_message: Начало текста ошибки, например «Письмо не прошло
            клиентскую валидацию».

    Returns:
        JSON-тело с алиасами полей API (`from`, а не `from_`) и без `None`:
        бэкенд `"html": null` примет, но шум в логах ни к чему.

    Raises:
        MailInfraError: Код `invalid_request`, разбор по полям — в
            `details["fields"]`.
    """
    try:
        validated = model.model_validate(payload)
    except PydanticValidationError as exc:
        # README учит ловить `MailInfraError`, и голый `ValidationError`
        # пролетал бы мимо такого except — ровно та же дыра, что уже закрыта
        # в `webhooks.construct_event`. Go и PHP отдают здесь свой
        # `invalid_request`, и без обёртки четыре SDK на одном и том же
        # письме требуют разных except.
        fields, summary = field_errors(exc)
        raise MailInfraError(
            f"{failure_message} — {summary}",
            code="invalid_request",
            details={"fields": fields},
        ) from exc
    return validated.model_dump(mode="json", by_alias=True, exclude_none=True)
