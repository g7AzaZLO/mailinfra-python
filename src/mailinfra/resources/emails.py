"""Ресурс писем: отправка, батч, чтение, постраничный обход, отмена и перенос.

Сборка и валидация тела запроса общая для синхронного и асинхронного
клиентов (`build_send_payload`), различается только способ сделать HTTP-вызов.

Об идемпотентности. `POST /emails` и `POST /emails/batch` принимают
`Idempotency-Key`, и SDK ставит его сам: ключ генерируется один раз на вызов и
переиспользуется всеми повторами внутри этого вызова. Поэтому сетевой сбой или
`503` не превращаются в два письма. Повторный вызов `send()` — это новый ключ и
новое письмо, как и ожидает вызывающий. У батча бэкенд выводит из ключа запроса
отдельный ключ для каждого письма: повтор не дублирует уже принятые письма, а
отклонённые пробует принять заново. Если ключа нет (`auto_idempotency=False`),
запрос помечается неидемпотентным: дедуплицировать повтор бэкенду нечем, и
повторяется только `429` — единственный отказ, который случается до приёма
писем.
"""

from __future__ import annotations

import base64
import builtins
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import quote
from uuid import UUID

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from .._errors import MailInfraError
from .._validation import RescheduleRequest, SendRequest, dump_validated, field_errors
from ..models import (
    Attachment,
    BatchSendResponseData,
    EmailDetail,
    EmailEventPublic,
    EmailListItem,
    EmailListResponse,
    EmailScheduleData,
    EmailSendResponseData,
    EmailStatus,
    SendBatchResult,
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

# Ключ батча короче на 3 символа: бэкенд дописывает к нему разделитель и номер
# письма (`\x1f0` … `\x1f99`), а ключ письма ограничен теми же 255 символами.
BATCH_IDEMPOTENCY_KEY_MAX_LENGTH = 252

# `BatchSendRequest.emails` в спеке ограничен `maxItems: 100`.
MAX_BATCH_SIZE = 100

_EMAILS_PATH = "/emails"
_BATCH_PATH = "/emails/batch"
_REQUEST_ID_HEADER = "X-Request-ID"

_SEND_REJECTED = "Письмо не прошло клиентскую валидацию"
_RESCHEDULE_REJECTED = "Новое время отправки не прошло клиентскую валидацию"

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
        fields, summary = field_errors(exc)
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
        "request_id": response.headers.get(_REQUEST_ID_HEADER),
    }


def _parse_accepted(
    model: type[_ModelT],
    data: Any,
    response: httpx.Response,
    idempotency_key: str | None,
) -> _ModelT:
    """Разбирает ответ на отправку, после которой письма уже приняты.

    Неразобранный `202` не значит «не отправлено»: повтор с новым ключом
    создал бы дубли. Поэтому ключ — в деталях ошибки, чтобы повторить вызов
    с ним же, а `request_id` — для поддержки.

    Args:
        model: Модель поля `data` ответа.
        data: Поле `data` ответа.
        response: Ответ httpx.
        idempotency_key: Ключ, с которым ушёл запрос.

    Returns:
        Экземпляр модели.

    Raises:
        MailInfraError: Ответ не совпал с моделью.
    """
    return _parse_response(
        model,
        data,
        **_response_meta(response),
        details={"idempotency_key": idempotency_key} if idempotency_key else None,
    )


def _acceptance_meta(response: httpx.Response, idempotency_key: str | None) -> dict[str, Any]:
    """Метаданные приёма для `SendEmailResult` и `SendBatchResult`.

    Args:
        response: Ответ на отправку.
        idempotency_key: Ключ, с которым ушёл запрос.

    Returns:
        `replayed`, `idempotency_key` и `request_id` — аргументы `from_response`.
    """
    replayed_header = response.headers.get(IDEMPOTENT_REPLAYED_HEADER) or ""
    return {
        # Первый приём — `202`, повтор — `200` с `Idempotent-Replayed: true`.
        "replayed": response.status_code == 200 or replayed_header.lower() == "true",
        "idempotency_key": idempotency_key,
        "request_id": response.headers.get(_REQUEST_ID_HEADER),
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


def _next_cursor(page: EmailListResponse) -> str | None:
    """Курсор страницы, которая идёт после `page`.

    Args:
        page: Полученная страница писем.

    Returns:
        `next_cursor` или `None`, если страниц больше нет.

    Raises:
        MailInfraError: `has_more` без `next_cursor`: запросить следующую
            страницу нечем, а молча оборвать обход значило бы потерять письма.
    """
    if not page.has_more:
        return None
    if not page.next_cursor:
        raise MailInfraError(
            "Ответ API не по контракту: has_more=true без next_cursor — обновите SDK",
            code="malformed_response",
        )
    return page.next_cursor


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
            с `filename` и `content_base64` (и, для inline-картинки,
            `content_id`).

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
            "content_id": item.content_id,
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
    metadata: Mapping[str, str] | None = None,
    template: str | None = None,
    variables: Mapping[str, Any] | None = None,
    attachments: Sequence[Attachment | Mapping[str, Any]] | None = None,
    send_at: datetime | str | None = None,
    ttl: int | None = None,
) -> dict[str, Any]:
    """Собирает и валидирует тело запроса на отправку.

    Лимиты спеки (получатели, теги, тема, вложения, диапазон `ttl`) и те, что
    в спеке не записаны (`metadata`, часовой пояс у `send_at`), проверяются до
    сетевого вызова — быстрее и понятнее, чем `422` от API.

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
        metadata: Ваши пары «ключ — значение»: до 20 ключей `[A-Za-z0-9_.-]`
            до 40 символов, значения до 500 символов, всё — до 4096 байт
            JSON. Вернутся в `get()`, `list()` и в вебхуках письма.
        template: Слаг шаблона вместо html/text.
        variables: Переменные шаблона.
        attachments: Вложения; с `content_id` — inline-картинки.
        send_at: Отложенная отправка: `datetime` с часовым поясом или ISO 8601
            строка со смещением, до 7 дней вперёд; прошедшее — сразу.
        ttl: Срок жизни письма в секундах, 60..86400, от `send_at` или от
            приёма; не доставленное за это время письмо не отправляется.

    Returns:
        Готовое JSON-тело запроса с алиасами полей API (`from`, а не
        `from_email`) и без пустых значений.

    Raises:
        MailInfraError: Письмо не прошло проверку: `code="invalid_request"`,
            разбор по полям — в `details["fields"]`.
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
        "metadata": dict(metadata) if metadata else None,
        "template": template,
        "variables": dict(variables) if variables else None,
        "attachments": (
            [_attachment_payload(item) for item in attachments] if attachments else None
        ),
        "send_at": send_at,
        "ttl": ttl,
    }
    payload = {key: value for key, value in raw.items() if value is not None}
    return dump_validated(SendRequest, payload, failure_message=_SEND_REJECTED)


def _reschedule_payload(send_at: datetime | str) -> dict[str, Any]:
    """Собирает и валидирует тело переноса письма.

    Args:
        send_at: Новое время отправки.

    Returns:
        JSON-тело для `PATCH /emails/{email_id}`.

    Raises:
        MailInfraError: У `send_at` нет часового пояса или он не разобран
            (`code="invalid_request"`).
    """
    return dump_validated(
        RescheduleRequest, {"send_at": send_at}, failure_message=_RESCHEDULE_REJECTED
    )


def _list_params(
    *,
    status: EmailStatus | str | None,
    from_email: str | None,
    to_email: str | None,
    tag: str | None,
    from_date: datetime | str | None,
    to_date: datetime | str | None,
    limit: int | None,
    cursor: str | None,
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
        limit: Размер страницы (1..100).
        cursor: `next_cursor` предыдущей страницы.

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
        "cursor": cursor,
    }
    return {key: value for key, value in raw.items() if value is not None}


class _EmailsResourceBase:
    """Общая логика ресурса писем без сетевых вызовов."""

    def _idempotency_headers(
        self, idempotency_key: str | None, *, auto_idempotency: bool, max_length: int
    ) -> tuple[dict[str, str], str | None]:
        """Готовит заголовок идемпотентности для отправки.

        Args:
            idempotency_key: Ключ от вызывающего; `None` — сгенерировать.
            auto_idempotency: Включена ли авто-генерация у клиента.
            max_length: Предел длины ключа у эндпоинта (у батча он короче).

        Returns:
            Пара «заголовки, использованный ключ». Ключ возвращается, чтобы
            попасть в результат отправки и в логи вызывающего.

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
        if len(idempotency_key) > max_length:
            raise MailInfraError(
                f"Idempotency-Key длиннее {max_length} символов — API отклонит такой запрос с 422",
                code="invalid_idempotency_key",
            )

        return {IDEMPOTENCY_KEY_HEADER: idempotency_key}, idempotency_key

    @staticmethod
    def _send_result(
        data: Any, response: httpx.Response, idempotency_key: str | None
    ) -> SendEmailResult:
        """Собирает результат отправки письма из ответа API.

        Args:
            data: Поле `data` ответа.
            response: Ответ httpx: статус и заголовки повтора.
            idempotency_key: Ключ, с которым ушёл запрос.

        Returns:
            Результат отправки.
        """
        parsed = _parse_accepted(EmailSendResponseData, data, response, idempotency_key)
        return SendEmailResult.from_response(parsed, **_acceptance_meta(response, idempotency_key))

    @staticmethod
    def _batch_result(
        data: Any, response: httpx.Response, idempotency_key: str | None
    ) -> SendBatchResult:
        """Собирает результат батча из ответа API.

        Args:
            data: Поле `data` ответа.
            response: Ответ httpx: статус и заголовки повтора.
            idempotency_key: Ключ, с которым ушёл запрос.

        Returns:
            Результат батча.
        """
        parsed = _parse_accepted(BatchSendResponseData, data, response, idempotency_key)
        return SendBatchResult.from_response(parsed, **_acceptance_meta(response, idempotency_key))

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
            # у send()», а `send()` принимает `idempotency_key`. У батча ключ
            # один на весь запрос, и тихо выбросить ключ письма нельзя: он ровно
            # для защиты от дублей и добавлен.
            if "idempotency_key" in fields:
                raise MailInfraError(
                    f"Письмо #{index} в батче задаёт idempotency_key, а у батча ключ один на "
                    "весь запрос: передайте его как send_batch(..., idempotency_key=...) — "
                    "бэкенд выведет из него ключ каждого письма.",
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
        metadata: Mapping[str, str] | None = None,
        template: str | None = None,
        variables: Mapping[str, Any] | None = None,
        attachments: Sequence[Attachment | Mapping[str, Any]] | None = None,
        send_at: datetime | str | None = None,
        ttl: int | None = None,
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
            metadata: Ваши пары «ключ — значение»; лимиты — в
                `build_send_payload`.
            template: Слаг шаблона.
            variables: Переменные шаблона.
            attachments: Вложения; с `content_id` — inline-картинки.
            send_at: Время отложенной отправки с часовым поясом, не дальше
                7 дней вперёд; письмо получит статус `SCHEDULED`.
            ttl: Срок жизни письма в секундах, 60..86400.
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
            metadata=metadata,
            template=template,
            variables=variables,
            attachments=attachments,
            send_at=send_at,
            ttl=ttl,
        )
        data, response, used_key = self._post_with_key(
            _EMAILS_PATH, payload, idempotency_key, max_key_length=IDEMPOTENCY_KEY_MAX_LENGTH
        )
        return self._send_result(data, response, used_key)

    def send_batch(
        self, emails: Sequence[Mapping[str, Any]], *, idempotency_key: str | None = None
    ) -> SendBatchResult:
        """Отправляет до 100 писем одним запросом.

        Батч возвращает `202` даже если часть писем не принята: проверяйте
        `error` у каждого элемента `results`, а не только статус ответа.
        `Idempotency-Key` SDK ставит сам и переиспользует его на повторах:
        бэкенд выводит из него ключ каждого письма, поэтому повтор после
        `5xx` или таймаута не дублирует уже принятые письма, а отклонённые
        пробует принять заново. Без ключа (`auto_idempotency=False`) батч
        повторяется только на `429`.

        Args:
            emails: Список наборов параметров, как у `send()`, но без
                `idempotency_key` — он у батча один на запрос.
            idempotency_key: Свой ключ батча, до 252 символов; по умолчанию
                SDK генерирует `uuid4` на каждый вызов.

        Returns:
            Результаты по каждому письму, счётчики принятых/отклонённых,
            флаг `replayed` и использованный ключ.

        Raises:
            MailInfraError: Ошибка API, транспорта или клиентской валидации.
        """
        payload = self._batch_payload(emails)
        data, response, used_key = self._post_with_key(
            _BATCH_PATH, payload, idempotency_key, max_key_length=BATCH_IDEMPOTENCY_KEY_MAX_LENGTH
        )
        return self._batch_result(data, response, used_key)

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
        cursor: str | None = None,
    ) -> EmailListResponse:
        """Возвращает одну страницу писем проекта по фильтрам, новые сначала.

        Обойти все страницы проще через `list_all()`.

        Args:
            status: Фильтр по статусу.
            from_email: Фильтр по отправителю.
            to_email: Фильтр по получателю.
            tag: Фильтр по тегу.
            from_date: Начало интервала по времени создания.
            to_date: Конец интервала.
            limit: Размер страницы, 1..100 (по умолчанию 25 на стороне API).
            cursor: `next_cursor` предыдущей страницы; фильтры передавайте
                те же. Без курсора — первая страница.

        Returns:
            Страница: письма в сокращённом виде (`data`), есть ли ещё
            (`has_more`) и курсор следующей страницы (`next_cursor`).
        """
        params = _list_params(
            status=status,
            from_email=from_email,
            to_email=to_email,
            tag=tag,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            cursor=cursor,
        )
        return self._page(params)

    def list_all(
        self,
        *,
        status: EmailStatus | str | None = None,
        from_email: str | None = None,
        to_email: str | None = None,
        tag: str | None = None,
        from_date: datetime | str | None = None,
        to_date: datetime | str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Iterator[EmailListItem]:
        """Обходит все письма по фильтрам, сам запрашивая страницы.

        Следующая страница запрашивается, только когда предыдущая выбрана до
        конца: прерванный цикл лишних запросов не делает.

        Args:
            status: Фильтр по статусу.
            from_email: Фильтр по отправителю.
            to_email: Фильтр по получателю.
            tag: Фильтр по тегу.
            from_date: Начало интервала по времени создания.
            to_date: Конец интервала.
            limit: Размер одной страницы, 1..100, а не общее число писем.
            cursor: С какой страницы начать — сохранённый ранее
                `next_cursor`; по умолчанию с самых новых писем.

        Yields:
            Письма в сокращённом виде, новые сначала.

        Raises:
            MailInfraError: Ошибка API на любой из страниц.
        """
        params = _list_params(
            status=status,
            from_email=from_email,
            to_email=to_email,
            tag=tag,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            cursor=cursor,
        )
        while True:
            page = self._page(params)
            yield from page.data
            next_cursor = _next_cursor(page)
            if next_cursor is None:
                return
            params = {**params, "cursor": next_cursor}

    def events(self, email_id: str | UUID) -> builtins.list[EmailEventPublic]:
        """Возвращает события письма в хронологическом порядке.

        Args:
            email_id: Идентификатор письма.

        Returns:
            Список событий (`scheduled`, `queued`, `sent`, `delivered`, …).
        """
        data, response = self._client.request_with_response("GET", _email_path(email_id, "/events"))
        return _parse_list(EmailEventPublic, data, **_response_meta(response))

    def cancel(self, email_id: str | UUID) -> EmailScheduleData:
        """Отменяет запланированное письмо; резерв месячной квоты возвращается.

        Нужно право на отправку (`FULL_ACCESS` или `SENDING_ONLY`).

        Args:
            email_id: Идентификатор письма в статусе `SCHEDULED`.

        Returns:
            Состояние письма: `status` — `CANCELED`.

        Raises:
            EmailNotScheduledError: Письмо уже не запланировано: ушло в
                очередь, отправлено или отменено (статус — в
                `details["status"]`). На повторе после таймаута это значит,
                что первая попытка письмо уже отменила.
            NotFoundError: Письма нет или оно принадлежит другому проекту.
        """
        data, response = self._client.request_with_response(
            "POST", _email_path(email_id, "/cancel")
        )
        return _parse_response(EmailScheduleData, data, **_response_meta(response))

    def reschedule(self, email_id: str | UUID, *, send_at: datetime | str) -> EmailScheduleData:
        """Переносит запланированное письмо на новое время.

        Нужно право на отправку (`FULL_ACCESS` или `SENDING_ONLY`).

        Args:
            email_id: Идентификатор письма в статусе `SCHEDULED`.
            send_at: Новое время: `datetime` с часовым поясом или ISO 8601
                строка со смещением, не дальше 7 дней от приёма письма (а не
                от переноса). Время в прошлом — отправить сейчас.

        Returns:
            Состояние письма с новым `scheduled_at`.

        Raises:
            MailInfraError: У `send_at` нет часового пояса — до сетевого
                вызова, `code="invalid_request"`.
            SendAtTooFarError: Новое время дальше горизонта.
            EmailNotScheduledError: Письмо уже не запланировано.
            NotFoundError: Письма нет или оно принадлежит другому проекту.
        """
        data, response = self._client.request_with_response(
            "PATCH", _email_path(email_id), json=_reschedule_payload(send_at)
        )
        return _parse_response(EmailScheduleData, data, **_response_meta(response))

    def _page(self, params: dict[str, Any]) -> EmailListResponse:
        """Запрашивает одну страницу писем.

        Args:
            params: Query-параметры из `_list_params`.

        Returns:
            Страница писем.
        """
        body, response = self._client.request_with_response(
            "GET", _EMAILS_PATH, params=params, envelope=True
        )
        return _parse_response(EmailListResponse, body, **_response_meta(response))

    def _post_with_key(
        self,
        path: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
        *,
        max_key_length: int,
    ) -> tuple[Any, httpx.Response, str | None]:
        """Отправляет письма с ключом идемпотентности.

        Args:
            path: `/emails` или `/emails/batch`.
            payload: Готовое JSON-тело.
            idempotency_key: Ключ от вызывающего; `None` — по настройке клиента.
            max_key_length: Предел длины ключа у эндпоинта.

        Returns:
            Поле `data` ответа, сам ответ и использованный ключ.
        """
        request_headers, used_key = self._idempotency_headers(
            idempotency_key,
            auto_idempotency=self._client.auto_idempotency,
            max_length=max_key_length,
        )
        # Без `Idempotency-Key` бэкенду нечем распознать повтор
        # (`email_service.send_email` дедуплицирует только по ключу), поэтому
        # такой запрос неидемпотентен: повтор после `5xx` создал бы дубли.
        data, response = self._client.request_with_response(
            "POST", path, json=payload, headers=request_headers, idempotent=used_key is not None
        )
        return data, response, used_key


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
        metadata: Mapping[str, str] | None = None,
        template: str | None = None,
        variables: Mapping[str, Any] | None = None,
        attachments: Sequence[Attachment | Mapping[str, Any]] | None = None,
        send_at: datetime | str | None = None,
        ttl: int | None = None,
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
            metadata: Ваши пары «ключ — значение».
            template: Слаг шаблона.
            variables: Переменные шаблона.
            attachments: Вложения; с `content_id` — inline-картинки.
            send_at: Время отложенной отправки с часовым поясом.
            ttl: Срок жизни письма в секундах, 60..86400.
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
            metadata=metadata,
            template=template,
            variables=variables,
            attachments=attachments,
            send_at=send_at,
            ttl=ttl,
        )
        data, response, used_key = await self._post_with_key(
            _EMAILS_PATH, payload, idempotency_key, max_key_length=IDEMPOTENCY_KEY_MAX_LENGTH
        )
        return self._send_result(data, response, used_key)

    async def send_batch(
        self, emails: Sequence[Mapping[str, Any]], *, idempotency_key: str | None = None
    ) -> SendBatchResult:
        """Отправляет до 100 писем одним запросом.

        Args:
            emails: Список наборов параметров, как у `send()`.
            idempotency_key: Свой ключ батча, до 252 символов.

        Returns:
            Результаты по каждому письму, счётчики и флаг `replayed`.

        Raises:
            MailInfraError: Ошибка API или транспорта.
        """
        payload = self._batch_payload(emails)
        data, response, used_key = await self._post_with_key(
            _BATCH_PATH, payload, idempotency_key, max_key_length=BATCH_IDEMPOTENCY_KEY_MAX_LENGTH
        )
        return self._batch_result(data, response, used_key)

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
        cursor: str | None = None,
    ) -> EmailListResponse:
        """Возвращает одну страницу писем проекта по фильтрам.

        Args:
            status: Фильтр по статусу.
            from_email: Фильтр по отправителю.
            to_email: Фильтр по получателю.
            tag: Фильтр по тегу.
            from_date: Начало интервала.
            to_date: Конец интервала.
            limit: Размер страницы, 1..100.
            cursor: `next_cursor` предыдущей страницы.

        Returns:
            Страница с `data`, `has_more` и `next_cursor`.
        """
        params = _list_params(
            status=status,
            from_email=from_email,
            to_email=to_email,
            tag=tag,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            cursor=cursor,
        )
        return await self._page(params)

    async def list_all(
        self,
        *,
        status: EmailStatus | str | None = None,
        from_email: str | None = None,
        to_email: str | None = None,
        tag: str | None = None,
        from_date: datetime | str | None = None,
        to_date: datetime | str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> AsyncIterator[EmailListItem]:
        """Обходит все письма по фильтрам (`async for`), сам запрашивая страницы.

        Args:
            status: Фильтр по статусу.
            from_email: Фильтр по отправителю.
            to_email: Фильтр по получателю.
            tag: Фильтр по тегу.
            from_date: Начало интервала.
            to_date: Конец интервала.
            limit: Размер одной страницы, 1..100.
            cursor: С какой страницы начать.

        Yields:
            Письма в сокращённом виде, новые сначала.

        Raises:
            MailInfraError: Ошибка API на любой из страниц.
        """
        params = _list_params(
            status=status,
            from_email=from_email,
            to_email=to_email,
            tag=tag,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            cursor=cursor,
        )
        while True:
            page = await self._page(params)
            for item in page.data:
                yield item
            next_cursor = _next_cursor(page)
            if next_cursor is None:
                return
            params = {**params, "cursor": next_cursor}

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

    async def cancel(self, email_id: str | UUID) -> EmailScheduleData:
        """Отменяет запланированное письмо.

        Args:
            email_id: Идентификатор письма в статусе `SCHEDULED`.

        Returns:
            Состояние письма: `status` — `CANCELED`.

        Raises:
            EmailNotScheduledError: Письмо уже не запланировано.
        """
        data, response = await self._client.request_with_response(
            "POST", _email_path(email_id, "/cancel")
        )
        return _parse_response(EmailScheduleData, data, **_response_meta(response))

    async def reschedule(
        self, email_id: str | UUID, *, send_at: datetime | str
    ) -> EmailScheduleData:
        """Переносит запланированное письмо на новое время.

        Args:
            email_id: Идентификатор письма в статусе `SCHEDULED`.
            send_at: Новое время с часовым поясом.

        Returns:
            Состояние письма с новым `scheduled_at`.

        Raises:
            MailInfraError: Клиентская валидация или ошибка API.
        """
        data, response = await self._client.request_with_response(
            "PATCH", _email_path(email_id), json=_reschedule_payload(send_at)
        )
        return _parse_response(EmailScheduleData, data, **_response_meta(response))

    async def _page(self, params: dict[str, Any]) -> EmailListResponse:
        """Запрашивает одну страницу писем.

        Args:
            params: Query-параметры из `_list_params`.

        Returns:
            Страница писем.
        """
        body, response = await self._client.request_with_response(
            "GET", _EMAILS_PATH, params=params, envelope=True
        )
        return _parse_response(EmailListResponse, body, **_response_meta(response))

    async def _post_with_key(
        self,
        path: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
        *,
        max_key_length: int,
    ) -> tuple[Any, httpx.Response, str | None]:
        """Отправляет письма с ключом идемпотентности.

        Args:
            path: `/emails` или `/emails/batch`.
            payload: Готовое JSON-тело.
            idempotency_key: Ключ от вызывающего; `None` — по настройке клиента.
            max_key_length: Предел длины ключа у эндпоинта.

        Returns:
            Поле `data` ответа, сам ответ и использованный ключ.
        """
        request_headers, used_key = self._idempotency_headers(
            idempotency_key,
            auto_idempotency=self._client.auto_idempotency,
            max_length=max_key_length,
        )
        data, response = await self._client.request_with_response(
            "POST", path, json=payload, headers=request_headers, idempotent=used_key is not None
        )
        return data, response, used_key
