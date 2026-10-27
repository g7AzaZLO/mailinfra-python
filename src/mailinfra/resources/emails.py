"""Ресурс писем: отправка, батч, чтение статусов и событий.

Сборка и валидация тела запроса общая для синхронного и асинхронного
клиентов (`build_send_payload`), различается только способ сделать HTTP-вызов.

Об идемпотентности. `POST /emails` принимает `Idempotency-Key`, и SDK ставит
его сам: ключ генерируется один раз на вызов и переиспользуется всеми
повторами внутри этого вызова. Поэтому сетевой сбой или `503` не превращаются
в два письма. Повторный вызов `send()` — это новый ключ и новое письмо, как и
ожидает вызывающий. Если ключа нет (`auto_idempotency=False`), запрос
помечается неидемпотентным: дедуплицировать повтор бэкенду нечем.
`POST /emails/batch` заголовок не принимает (бэкенд коммитит письма батча по
одному), поэтому он неидемпотентен всегда и повторяется только на `429` —
единственном отказе, который случается до приёма писем.
"""

from __future__ import annotations

import base64
import builtins
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import quote
from uuid import UUID

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from .._errors import MailInfraError
from ..models import (
    Attachment,
    BatchSendResponseData,
    EmailDetail,
    EmailEventPublic,
    EmailListItem,
    EmailSendRequest,
    EmailSendResponseData,
    EmailStatus,
    SendEmailResult,
)

if TYPE_CHECKING:  # pragma: no cover — только для аннотаций
    import httpx

    from ..client import AsyncMailInfra, MailInfra

__all__ = ["AsyncEmailsResource", "EmailsResource", "build_send_payload"]

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
IDEMPOTENT_REPLAYED_HEADER = "Idempotent-Replayed"

# Бэкенд отклоняет ключ длиннее 255 символов (`IDEMPOTENCY_KEY_MAX_LENGTH`)
# с 422 — ловим это на клиенте, чтобы не тратить запрос.
IDEMPOTENCY_KEY_MAX_LENGTH = 255

# `BatchSendRequest.emails` в спеке ограничен `maxItems: 100`.
MAX_BATCH_SIZE = 100

_EMAILS_PATH = "/emails"
_BATCH_PATH = "/emails/batch"

_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _parse_response(
    model: type[_ModelT],
    data: Any,
    *,
    status_code: int | None = None,
    request_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> _ModelT:
    """Разбирает ответ API в модель SDK.

    Бэкенд может прислать значение, которого эта версия SDK не знает (новый
    статус письма или тип события). Голый `pydantic.ValidationError` пролетал
    мимо `except MailInfraError` из README (аудит S1-22), поэтому ошибка
    разбора — тоже `MailInfraError` с кодом `malformed_response`.

    Args:
        model: Модель ответа.
        data: Поле `data` ответа.
        status_code: HTTP-статус ответа, если вызывающий его знает.
        request_id: `X-Request-ID` ответа — для обращения в поддержку.
        details: Дополнительные детали ошибки (например, ключ идемпотентности).

    Returns:
        Экземпляр модели.

    Raises:
        MailInfraError: Ответ не совпал с моделью — обычно пора обновить SDK.
    """
    try:
        return model.model_validate(data)
    except PydanticValidationError as exc:
        fields = [
            {"field": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
            for error in exc.errors()
        ]
        summary = "; ".join(f"{item['field']}: {item['message']}" for item in fields)
        raise MailInfraError(
            f"Ответ API не совпал с моделью {model.__name__} — обновите SDK ({summary})",
            status_code=status_code,
            code="malformed_response",
            details={**(details or {}), "fields": fields},
            request_id=request_id,
        ) from exc


def _response_meta(response: httpx.Response) -> dict[str, Any]:
    """`status_code` и `request_id` ответа — для ошибки `malformed_response`."""
    return {
        "status_code": response.status_code,
        "request_id": response.headers.get("X-Request-ID"),
    }


def _parse_list(
    model: type[_ModelT],
    data: Any,
    *,
    status_code: int | None = None,
    request_id: str | None = None,
) -> builtins.list[_ModelT]:
    """Разбирает список из ответа API; не-список — тоже `malformed_response`.

    Args:
        model: Модель элемента.
        data: Поле `data` ответа.
        status_code: HTTP-статус ответа.
        request_id: `X-Request-ID` ответа.

    Returns:
        Элементы списка.

    Raises:
        MailInfraError: `data` не список или элемент не совпал с моделью.
    """
    if not isinstance(data, list):
        raise MailInfraError(
            f"Ответ API: ожидался список {model.__name__}, пришло {type(data).__name__} "
            "— обновите SDK",
            status_code=status_code,
            code="malformed_response",
            request_id=request_id,
        )
    return [
        _parse_response(model, item, status_code=status_code, request_id=request_id)
        for item in data
    ]


def _email_path(email_id: str | UUID, suffix: str = "") -> str:
    """Собирает путь к письму, экранируя идентификатор.

    `email_id` приходит снаружи, и без экранирования значение со слешем или
    `?` молча превратилось бы в запрос к другому эндпоинту.

    Args:
        email_id: Идентификатор письма.
        suffix: Хвост пути, например `/events`.

    Returns:
        Путь относительно базы API.
    """
    return f"{_EMAILS_PATH}/{quote(str(email_id), safe='')}{suffix}"


def _as_list(value: str | Sequence[str] | None) -> list[str] | None:
    """Приводит адресата к списку.

    `to="user@example.com"` — самый частый вызов, и требовать от него список
    было бы мелочной придиркой.

    Args:
        value: Адрес, последовательность адресов или `None`.

    Returns:
        Список адресов либо `None`.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return list(value)


def _attachment_payload(item: Attachment | Mapping[str, Any]) -> dict[str, Any]:
    """Приводит вложение к виду, который ждёт API.

    Args:
        item: `Attachment` (SDK сам закодирует байты) либо готовый словарь
            с `filename` и `content_base64`.

    Returns:
        Словарь для поля `attachments`.

    Raises:
        ValueError: Если в словаре нет ни `content`, ни `content_base64`.
    """
    if isinstance(item, Attachment):
        content_base64 = item.content_base64
        if content_base64 is None and item.content is not None:
            content_base64 = base64.b64encode(item.content).decode("ascii")
        return {
            "filename": item.filename,
            "content_base64": content_base64,
            "content_type": item.content_type,
        }

    payload = dict(item)
    if "content_base64" not in payload:
        content = payload.pop("content", None)
        if content is None:
            raise ValueError("Вложению нужен content (bytes) или content_base64 (str)")
        if isinstance(content, str):
            content = content.encode("utf-8")
        payload["content_base64"] = base64.b64encode(content).decode("ascii")
    return payload


def build_send_payload(
    *,
    from_email: str,
    to: str | Sequence[str],
    subject: str | None = None,
    html: str | None = None,
    text: str | None = None,
    from_name: str | None = None,
    cc: str | Sequence[str] | None = None,
    bcc: str | Sequence[str] | None = None,
    reply_to: str | Sequence[str] | None = None,
    headers: Mapping[str, str] | None = None,
    tags: Sequence[str] | None = None,
    template: str | None = None,
    variables: Mapping[str, Any] | None = None,
    attachments: Sequence[Attachment | Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Собирает и валидирует тело запроса на отправку.

    Валидация идёт через сгенерированную из OpenAPI модель
    `EmailSendRequest`: лимиты (50 получателей, 10 тегов, 998 символов в
    subject, размер вложения) проверяются до сетевого вызова — быстрее и
    понятнее, чем `422` от API. Ошибка приходит как `MailInfraError` с
    кодом `invalid_request`, а разбор по полям лежит в `details["fields"]`.

    Args:
        from_email: Адрес отправителя на подтверждённом домене.
        to: Получатель или список получателей.
        subject: Тема. Обязательна, если не используется шаблон.
        html: HTML-часть письма.
        text: Текстовая часть письма.
        from_name: Отображаемое имя отправителя.
        cc: Копия.
        bcc: Скрытая копия.
        reply_to: Адреса для ответа.
        headers: Дополнительные заголовки письма (системные запрещены).
        tags: Теги для фильтрации в дашборде и вебхуках.
        template: Слаг шаблона вместо html/text.
        variables: Переменные шаблона.
        attachments: Вложения.

    Returns:
        Готовое JSON-тело запроса с алиасами полей API (`from`, а не
        `from_email`) и без пустых значений.
    """
    raw: dict[str, Any] = {
        "from": from_email,
        "from_name": from_name,
        "to": _as_list(to),
        "cc": _as_list(cc),
        "bcc": _as_list(bcc),
        "reply_to": _as_list(reply_to),
        "subject": subject,
        "html": html,
        "text": text,
        "headers": dict(headers) if headers else None,
        "tags": list(tags) if tags else None,
        "template": template,
        "variables": dict(variables) if variables else None,
        "attachments": (
            [_attachment_payload(item) for item in attachments] if attachments else None
        ),
    }
    payload = {key: value for key, value in raw.items() if value is not None}

    # Валидация по спеке. `exclude_none` в дампе нужен, чтобы не отправлять
    # `"html": null` — бэкенд это примет, но шум в логах ни к чему.
    try:
        model = EmailSendRequest.model_validate(payload)
    except PydanticValidationError as exc:
        # README учит ловить `MailInfraError`, и голый `ValidationError`
        # пролетал бы мимо такого except — ровно та же дыра, что уже закрыта
        # в `webhooks.construct_event`. Go и PHP отдают здесь свой
        # `invalid_request`, и без обёртки четыре SDK на одном и том же
        # письме требуют разных except.
        fields = [
            {
                "field": ".".join(str(part) for part in error["loc"]),
                "message": error["msg"],
            }
            for error in exc.errors()
        ]
        summary = "; ".join(f"{item['field']}: {item['message']}" for item in fields)
        raise MailInfraError(
            f"Письмо не прошло клиентскую валидацию — {summary}",
            code="invalid_request",
            details={"fields": fields},
        ) from exc
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def _list_params(
    *,
    status: EmailStatus | str | None,
    from_email: str | None,
    to_email: str | None,
    tag: str | None,
    from_date: datetime | str | None,
    to_date: datetime | str | None,
    limit: int | None,
) -> dict[str, Any]:
    """Собирает query-параметры для списка писем.

    Имена `from`/`to` в query конфликтуют с ключевым словом и с полями
    письма, поэтому наружу они выставлены как `from_date`/`to_date`.

    Args:
        status: Фильтр по статусу.
        from_email: Фильтр по отправителю.
        to_email: Фильтр по получателю.
        tag: Фильтр по тегу.
        from_date: Начало интервала по `created_at`. Наивный `datetime`
            трактуется как локальное время и переводится в UTC.
        to_date: Конец интервала по `created_at`; та же трактовка времени.
        limit: Размер выборки (1..100).

    Returns:
        Словарь query-параметров без пустых значений.
    """

    def _moment(value: datetime | str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            return value
        # `isoformat()` наивного datetime уезжал без смещения, и API читал
        # локальное время как UTC — окно фильтра съезжало на часовой пояс
        # хоста. Наивный трактуем как локальный (так же, как его создаёт
        # `datetime.now()`), aware — переводим в UTC; Node в обоих случаях
        # шлёт `toISOString()`, то есть UTC.
        return value.astimezone(timezone.utc).isoformat()

    raw: dict[str, Any] = {
        "status": status.value if isinstance(status, EmailStatus) else status,
        "from_email": from_email,
        "to_email": to_email,
        "tag": tag,
        "from": _moment(from_date),
        "to": _moment(to_date),
        "limit": limit,
    }
    return {key: value for key, value in raw.items() if value is not None}


class _EmailsResourceBase:
    """Общая логика ресурса писем без сетевых вызовов."""

    def _idempotency_headers(
        self, idempotency_key: str | None, *, auto_idempotency: bool
    ) -> tuple[dict[str, str], str | None]:
        """Готовит заголовок идемпотентности для отправки.

        Args:
            idempotency_key: Ключ от вызывающего; `None` — сгенерировать.
            auto_idempotency: Включена ли авто-генерация у клиента.

        Returns:
            Пара «заголовки, использованный ключ». Ключ возвращается, чтобы
            попасть в `SendEmailResult` и в логи вызывающего.

        Raises:
            MailInfraError: Если переданный ключ пуст или длиннее лимита API.
        """
        if idempotency_key is None and auto_idempotency:
            idempotency_key = str(uuid.uuid4())

        if idempotency_key is None:
            return {}, None

        # Пустой ключ бэкенд не видит: дедупликация в
        # `email_service.send_email` стоит под `if payload.idempotency_key:`, и
        # пустая строка туда не проходит. Молча отправить такой запрос нельзя —
        # он помечается идемпотентным, повторяется после `5xx` и создаёт второе
        # письмо. Отправка без ключа — это `auto_idempotency=False` у клиента.
        if not idempotency_key.strip():
            raise MailInfraError(
                "Пустой Idempotency-Key: бэкенд его игнорирует, а повтор после 5xx создал бы "
                "второе письмо. Передайте непустой ключ или создайте клиент с "
                "auto_idempotency=False, чтобы отправлять без него.",
                code="invalid_idempotency_key",
            )

        # Значение заголовка уходит байтами, а бэкенд читает их как latin-1:
        # 255 кириллических символов приезжают к нему как 510 и получают 422.
        # До этого дело даже не доходит: httpx кодирует значение в ascii и
        # падает UnicodeEncodeError, а голое исключение пролетело бы мимо
        # `except MailInfraError`. Управляющие символы отбиваем заодно: CR и
        # LF в значении заголовка — это инъекция заголовков.
        if not all("\x20" <= char <= "\x7e" for char in idempotency_key):
            raise MailInfraError(
                "Idempotency-Key должен состоять из печатаемых ASCII-символов: значение "
                "заголовка уходит байтами, и бэкенд читает его как latin-1 — кириллица и "
                "эмодзи до него не доедут",
                code="invalid_idempotency_key",
            )

        # Ошибка того же класса, что и остальные: README учит ловить
        # `MailInfraError`, и ValueError пролетал бы мимо такого except.
        if len(idempotency_key) > IDEMPOTENCY_KEY_MAX_LENGTH:
            raise MailInfraError(
                f"Idempotency-Key длиннее {IDEMPOTENCY_KEY_MAX_LENGTH} символов — "
                "API отклонит такой запрос с 422",
                code="invalid_idempotency_key",
            )

        return {IDEMPOTENCY_KEY_HEADER: idempotency_key}, idempotency_key

    @staticmethod
    def _send_result(
        data: Any,
        *,
        status_code: int,
        replayed_header: str | None,
        request_id: str | None,
        idempotency_key: str | None,
    ) -> SendEmailResult:
        """Собирает результат отправки из ответа API.

        Args:
            data: Поле `data` ответа.
            status_code: HTTP-статус: `202` — принято, `200` — повтор.
            replayed_header: Значение заголовка `Idempotent-Replayed`.
            request_id: Значение заголовка `X-Request-ID`.
            idempotency_key: Ключ, с которым ушёл запрос.

        Returns:
            Результат отправки.
        """
        replayed = status_code == 200 or (replayed_header or "").lower() == "true"
        # Письмо уже принято, но ответ не разобран: ключ в деталях ошибки, чтобы
        # повторить send() с ним же, а не получить дубль с новым ключом.
        parsed = _parse_response(
            EmailSendResponseData,
            data,
            status_code=status_code,
            request_id=request_id,
            details={"idempotency_key": idempotency_key} if idempotency_key else None,
        )
        return SendEmailResult.from_response(
            parsed,
            replayed=replayed,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

    @staticmethod
    def _batch_payload(
        emails: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Собирает тело батча из списка наборов параметров.

        Args:
            emails: Список словарей с теми же ключами, что у `send()`
                (`from_email`, `to`, `subject`, …).

        Returns:
            JSON-тело для `POST /emails/batch`.

        Raises:
            MailInfraError: Если список пуст, длиннее лимита API или элемент
                содержит параметр, которого `POST /emails/batch` не принимает.
                API вернул бы 422, но падать до запроса честнее — батч с
                вложениями весит до ~25 МиБ на письмо.
        """
        if not emails:
            raise MailInfraError(
                "Пустой батч: передайте хотя бы одно письмо",
                code="empty_batch",
            )
        if len(emails) > MAX_BATCH_SIZE:
            raise MailInfraError(
                f"В батче {len(emails)} писем, а API принимает не больше {MAX_BATCH_SIZE} за раз",
                code="batch_too_large",
            )

        payloads = []
        for index, item in enumerate(emails):
            fields = dict(item)
            # Документация описывает элементы батча как «наборы параметров, как
            # у send()», а `send()` принимает `idempotency_key`. Эндпоинт батча
            # заголовок не принимает вовсе, и тихо выбросить ключ нельзя: он
            # ровно для защиты от дублей и добавлен.
            if "idempotency_key" in fields:
                raise MailInfraError(
                    f"Письмо #{index} в батче задаёт idempotency_key, а "
                    "POST /emails/batch этот заголовок не принимает — бэкенд коммитит письма "
                    "батча по одному. Отправляйте такие письма через send().",
                    code="invalid_batch_item",
                )
            try:
                payloads.append(build_send_payload(**fields))
            except TypeError as exc:
                # Голый TypeError не ловится ни одним из документированных
                # except-ов вызывающего.
                raise MailInfraError(
                    f"Письмо #{index} в батче содержит неизвестный параметр: {exc}",
                    code="invalid_batch_item",
                ) from exc
        return {"emails": payloads}


class EmailsResource(_EmailsResourceBase):
    """Письма: синхронный вариант.

    Доступен как `client.emails`.
    """

    def __init__(self, client: MailInfra) -> None:
        self._client = client

    def send(
        self,
        *,
        from_email: str,
        to: str | Sequence[str],
        subject: str | None = None,
        html: str | None = None,
        text: str | None = None,
        from_name: str | None = None,
        cc: str | Sequence[str] | None = None,
        bcc: str | Sequence[str] | None = None,
        reply_to: str | Sequence[str] | None = None,
        headers: Mapping[str, str] | None = None,
        tags: Sequence[str] | None = None,
        template: str | None = None,
        variables: Mapping[str, Any] | None = None,
        attachments: Sequence[Attachment | Mapping[str, Any]] | None = None,
        idempotency_key: str | None = None,
    ) -> SendEmailResult:
        """Отправляет письмо.

        Args:
            from_email: Адрес отправителя на подтверждённом домене.
            to: Получатель или список получателей.
            subject: Тема; обязательна без шаблона.
            html: HTML-часть.
            text: Текстовая часть.
            from_name: Отображаемое имя отправителя.
            cc: Копия.
            bcc: Скрытая копия.
            reply_to: Адреса для ответа.
            headers: Дополнительные заголовки письма.
            tags: Теги.
            template: Слаг шаблона.
            variables: Переменные шаблона.
            attachments: Вложения.
            idempotency_key: Свой ключ идемпотентности; по умолчанию SDK
                генерирует `uuid4` на каждый вызов.

        Returns:
            Результат с `id`, `status`, флагом `replayed` и использованным
            ключом идемпотентности.

        Raises:
            MailInfraError: Ошибка API, транспорта или клиентской валидации
                (в последнем случае `code="invalid_request"`, а разбор по
                полям — в `details["fields"]`).
        """
        payload = build_send_payload(
            from_email=from_email,
            to=to,
            subject=subject,
            html=html,
            text=text,
            from_name=from_name,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            headers=headers,
            tags=tags,
            template=template,
            variables=variables,
            attachments=attachments,
        )
        request_headers, used_key = self._idempotency_headers(
            idempotency_key, auto_idempotency=self._client.auto_idempotency
        )

        # Без `Idempotency-Key` бэкенду нечем распознать повтор
        # (`email_service.send_email` дедуплицирует только по ключу), поэтому
        # такой запрос неидемпотентен: повтор после `5xx` создал бы второе письмо.
        data, response = self._client.request_with_response(
            "POST",
            _EMAILS_PATH,
            json=payload,
            headers=request_headers,
            idempotent=used_key is not None,
        )
        return self._send_result(
            data,
            status_code=response.status_code,
            replayed_header=response.headers.get(IDEMPOTENT_REPLAYED_HEADER),
            request_id=response.headers.get("X-Request-ID"),
            idempotency_key=used_key,
        )

    def send_batch(self, emails: Sequence[Mapping[str, Any]]) -> BatchSendResponseData:
        """Отправляет до 100 писем одним запросом.

        Батч возвращает `202` даже если часть писем не принята: проверяйте
        `error` у каждого элемента `results`, а не только статус ответа.
        `Idempotency-Key` этот эндпоинт не поддерживает, поэтому SDK не
        повторяет его после `5xx`, таймаута и обрыва соединения — повтор мог
        бы отправить часть писем дважды. Повторяется только `429`.

        Args:
            emails: Список наборов параметров, как у `send()`.

        Returns:
            Результаты по каждому письму и счётчики принятых/отклонённых.

        Raises:
            MailInfraError: Ошибка API или транспорта.
        """
        payload = self._batch_payload(emails)
        # Письма батча уже приняты, а ключа идемпотентности у батча нет: повтор
        # создал бы дубли — в ошибке разбора нужен request_id для поддержки.
        data, response = self._client.request_with_response(
            "POST", _BATCH_PATH, json=payload, idempotent=False
        )
        return _parse_response(BatchSendResponseData, data, **_response_meta(response))

    def get(self, email_id: str | UUID) -> EmailDetail:
        """Возвращает письмо с телом, заголовками и диагностикой доставки.

        Args:
            email_id: Идентификатор письма.

        Returns:
            Подробности письма.

        Raises:
            NotFoundError: Письма нет или оно принадлежит другому проекту.
        """
        data, response = self._client.request_with_response("GET", _email_path(email_id))
        return _parse_response(EmailDetail, data, **_response_meta(response))

    def list(
        self,
        *,
        status: EmailStatus | str | None = None,
        from_email: str | None = None,
        to_email: str | None = None,
        tag: str | None = None,
        from_date: datetime | str | None = None,
        to_date: datetime | str | None = None,
        limit: int | None = None,
    ) -> builtins.list[EmailListItem]:
        """Возвращает письма проекта по фильтрам.

        Args:
            status: Фильтр по статусу.
            from_email: Фильтр по отправителю.
            to_email: Фильтр по получателю.
            tag: Фильтр по тегу.
            from_date: Начало интервала по времени создания.
            to_date: Конец интервала.
            limit: Размер выборки, 1..100 (по умолчанию 25 на стороне API).

        Returns:
            Список писем в сокращённом виде.
        """
        params = _list_params(
            status=status,
            from_email=from_email,
            to_email=to_email,
            tag=tag,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
        )
        data, response = self._client.request_with_response("GET", _EMAILS_PATH, params=params)
        return _parse_list(EmailListItem, data, **_response_meta(response))

    def events(self, email_id: str | UUID) -> builtins.list[EmailEventPublic]:
        """Возвращает события письма в хронологическом порядке.

        Args:
            email_id: Идентификатор письма.

        Returns:
            Список событий (`queued`, `sent`, `delivered`, `bounced`, …).
        """
        data, response = self._client.request_with_response("GET", _email_path(email_id, "/events"))
        return _parse_list(EmailEventPublic, data, **_response_meta(response))


class AsyncEmailsResource(_EmailsResourceBase):
    """Письма: асинхронный вариант.

    Доступен как `client.emails` у `AsyncMailInfra`.
    """

    def __init__(self, client: AsyncMailInfra) -> None:
        self._client = client

    async def send(
        self,
        *,
        from_email: str,
        to: str | Sequence[str],
        subject: str | None = None,
        html: str | None = None,
        text: str | None = None,
        from_name: str | None = None,
        cc: str | Sequence[str] | None = None,
        bcc: str | Sequence[str] | None = None,
        reply_to: str | Sequence[str] | None = None,
        headers: Mapping[str, str] | None = None,
        tags: Sequence[str] | None = None,
        template: str | None = None,
        variables: Mapping[str, Any] | None = None,
        attachments: Sequence[Attachment | Mapping[str, Any]] | None = None,
        idempotency_key: str | None = None,
    ) -> SendEmailResult:
        """Отправляет письмо.

        Args:
            from_email: Адрес отправителя на подтверждённом домене.
            to: Получатель или список получателей.
            subject: Тема; обязательна без шаблона.
            html: HTML-часть.
            text: Текстовая часть.
            from_name: Отображаемое имя отправителя.
            cc: Копия.
            bcc: Скрытая копия.
            reply_to: Адреса для ответа.
            headers: Дополнительные заголовки письма.
            tags: Теги.
            template: Слаг шаблона.
            variables: Переменные шаблона.
            attachments: Вложения.
            idempotency_key: Свой ключ идемпотентности.

        Returns:
            Результат отправки.

        Raises:
            MailInfraError: Ошибка API или транспорта.
        """
        payload = build_send_payload(
            from_email=from_email,
            to=to,
            subject=subject,
            html=html,
            text=text,
            from_name=from_name,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            headers=headers,
            tags=tags,
            template=template,
            variables=variables,
            attachments=attachments,
        )
        request_headers, used_key = self._idempotency_headers(
            idempotency_key, auto_idempotency=self._client.auto_idempotency
        )

        data, response = await self._client.request_with_response(
            "POST",
            _EMAILS_PATH,
            json=payload,
            headers=request_headers,
            idempotent=used_key is not None,
        )
        return self._send_result(
            data,
            status_code=response.status_code,
            replayed_header=response.headers.get(IDEMPOTENT_REPLAYED_HEADER),
            request_id=response.headers.get("X-Request-ID"),
            idempotency_key=used_key,
        )

    async def send_batch(self, emails: Sequence[Mapping[str, Any]]) -> BatchSendResponseData:
        """Отправляет до 100 писем одним запросом.

        Args:
            emails: Список наборов параметров, как у `send()`.

        Returns:
            Результаты по каждому письму и счётчики.

        Raises:
            MailInfraError: Ошибка API или транспорта.
        """
        payload = self._batch_payload(emails)
        # Письма батча уже приняты, а ключа идемпотентности у батча нет: повтор
        # создал бы дубли — в ошибке разбора нужен request_id для поддержки.
        data, response = await self._client.request_with_response(
            "POST", _BATCH_PATH, json=payload, idempotent=False
        )
        return _parse_response(BatchSendResponseData, data, **_response_meta(response))

    async def get(self, email_id: str | UUID) -> EmailDetail:
        """Возвращает письмо с телом, заголовками и диагностикой доставки.

        Args:
            email_id: Идентификатор письма.

        Returns:
            Подробности письма.
        """
        data, response = await self._client.request_with_response("GET", _email_path(email_id))
        return _parse_response(EmailDetail, data, **_response_meta(response))

    async def list(
        self,
        *,
        status: EmailStatus | str | None = None,
        from_email: str | None = None,
        to_email: str | None = None,
        tag: str | None = None,
        from_date: datetime | str | None = None,
        to_date: datetime | str | None = None,
        limit: int | None = None,
    ) -> builtins.list[EmailListItem]:
        """Возвращает письма проекта по фильтрам.

        Args:
            status: Фильтр по статусу.
            from_email: Фильтр по отправителю.
            to_email: Фильтр по получателю.
            tag: Фильтр по тегу.
            from_date: Начало интервала.
            to_date: Конец интервала.
            limit: Размер выборки, 1..100.

        Returns:
            Список писем в сокращённом виде.
        """
        params = _list_params(
            status=status,
            from_email=from_email,
            to_email=to_email,
            tag=tag,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
        )
        data, response = await self._client.request_with_response(
            "GET", _EMAILS_PATH, params=params
        )
        return _parse_list(EmailListItem, data, **_response_meta(response))

    async def events(self, email_id: str | UUID) -> builtins.list[EmailEventPublic]:
        """Возвращает события письма в хронологическом порядке.

        Args:
            email_id: Идентификатор письма.

        Returns:
            Список событий.
        """
        data, response = await self._client.request_with_response(
            "GET", _email_path(email_id, "/events")
        )
        return _parse_list(EmailEventPublic, data, **_response_meta(response))
