"""Покрытие операций спеки методами SDK.

Смысл теста: новый эндпоинт в публичном API нельзя добавить и забыть про
SDK. Спека перегенерируется из FastAPI, в ней появляется операция — и этот
тест краснеет, пока у неё нет метода в клиенте.

Заодно проверяются пути и признак идемпотентности: `Idempotency-Key` описан
в спеке у `POST /emails` и `POST /emails/batch`, и именно так SDK и должен
считать.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from mailinfra.resources.emails import (
    BATCH_IDEMPOTENCY_KEY_MAX_LENGTH,
    IDEMPOTENCY_KEY_HEADER,
    IDEMPOTENCY_KEY_MAX_LENGTH,
    AsyncEmailsResource,
    EmailsResource,
    _list_params,
    build_send_payload,
)

# operationId в спеке → имя метода ресурса писем.
OPERATION_METHODS: dict[str, str] = {
    "send_email": "send",
    "send_batch": "send_batch",
    "get_email": "get",
    "list_emails": "list",
    "list_email_events": "events",
    "cancel_email": "cancel",
    "reschedule_email": "reschedule",
}

# Методы без своей операции в спеке: обёртки над операциями выше.
HELPER_METHODS: tuple[str, ...] = ("list_all",)

# Эндпоинты, которые принимают `Idempotency-Key`, и предел длины ключа у SDK.
IDEMPOTENT_ENDPOINTS: dict[tuple[str, str], int] = {
    ("/emails", "post"): IDEMPOTENCY_KEY_MAX_LENGTH,
    ("/emails/batch", "post"): BATCH_IDEMPOTENCY_KEY_MAX_LENGTH,
}


def _operations(spec: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Собирает операции спеки.

    Args:
        spec: Документ спеки.

    Returns:
        Карта «operationId → (путь, метод)».
    """
    result: dict[str, tuple[str, str]] = {}
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if isinstance(operation, dict) and "operationId" in operation:
                result[operation["operationId"]] = (path, method)
    return result


def test_every_operation_has_sdk_method(spec: dict[str, Any]) -> None:
    operations = _operations(spec)

    unmapped = set(operations) - set(OPERATION_METHODS)
    assert not unmapped, (
        f"Операции публичного API без метода в SDK: {sorted(unmapped)}. "
        "Добавьте метод в mailinfra.resources.emails и запись в OPERATION_METHODS."
    )

    stale = set(OPERATION_METHODS) - set(operations)
    assert not stale, f"В карте есть операции, которых больше нет в спеке: {sorted(stale)}"


@pytest.mark.parametrize("resource", [EmailsResource, AsyncEmailsResource])
def test_methods_exist_on_both_clients(resource: type, spec: dict[str, Any]) -> None:
    for operation_id, method_name in OPERATION_METHODS.items():
        assert callable(getattr(resource, method_name, None)), (
            f"{resource.__name__} не реализует {method_name} для операции {operation_id}"
        )
    for method_name in HELPER_METHODS:
        assert callable(getattr(resource, method_name, None)), (
            f"{resource.__name__} не реализует {method_name}"
        )


@pytest.mark.parametrize("method_name", sorted({*OPERATION_METHODS.values(), *HELPER_METHODS}))
def test_sync_and_async_signatures_match(method_name: str) -> None:
    """Синхронный и асинхронный ресурсы — построчные близнецы, и это осознанно.

    Явные сигнатуры нужны для типизации и подсказок IDE, свести их в одну
    (`Unpack[TypedDict]`) нельзя без `typing_extensions` на Python 3.10, а
    третья зависимость библиотеке дороже дублирования. Но расхождение обязано
    быть громким: поле, добавленное только в синхронный `send()`, иначе молча
    оставит async-клиент неспособным его отправить.
    """
    sync = inspect.signature(getattr(EmailsResource, method_name))
    asyn = inspect.signature(getattr(AsyncEmailsResource, method_name))

    assert sync.parameters == asyn.parameters, (
        f"Сигнатуры {method_name}() разошлись между EmailsResource и AsyncEmailsResource"
    )


def test_send_forwards_every_payload_field() -> None:
    """`send()` обязан принимать всё, что умеет собрать `build_send_payload`."""
    payload_fields = set(inspect.signature(build_send_payload).parameters)
    send_fields = set(inspect.signature(EmailsResource.send).parameters) - {
        "self",
        "idempotency_key",
    }

    assert send_fields == payload_fields, (
        "send() и build_send_payload() разошлись по набору полей письма: "
        f"лишние {sorted(send_fields - payload_fields)}, "
        f"забытые {sorted(payload_fields - send_fields)}"
    )


@pytest.mark.parametrize("method_name", ["list", "list_all"])
def test_list_forwards_every_filter(method_name: str) -> None:
    """То же для фильтров списка — и у страницы, и у обхода всех страниц."""
    filter_fields = set(inspect.signature(_list_params).parameters)
    method = getattr(EmailsResource, method_name)
    list_fields = set(inspect.signature(method).parameters) - {"self"}

    assert list_fields == filter_fields


def test_list_accepts_cursor_from_spec(spec: dict[str, Any]) -> None:
    """Курсорная пагинация: SDK обязан уметь передать `cursor` в query."""
    query = {
        parameter["name"]
        for parameter in spec["paths"]["/emails"]["get"]["parameters"]
        if parameter.get("in") == "query"
    }

    assert "cursor" in query
    assert "cursor" in inspect.signature(_list_params).parameters


def test_operation_ids_are_clean(spec: dict[str, Any]) -> None:
    """`operationId` не должен тащить в себе путь и метод.

    Иначе кодогенераторы делают из него имена вида
    `send_email_v1_emails_post`, и SDK на других языках получает нечитаемый
    API.
    """
    for operation_id in _operations(spec):
        assert "_v1_" not in operation_id
        assert not operation_id.endswith(("_post", "_get"))


def test_paths_used_by_sdk_match_spec(spec: dict[str, Any]) -> None:
    operations = _operations(spec)

    assert operations["send_email"] == ("/emails", "post")
    assert operations["send_batch"] == ("/emails/batch", "post")
    assert operations["get_email"] == ("/emails/{email_id}", "get")
    assert operations["list_emails"] == ("/emails", "get")
    assert operations["list_email_events"] == ("/emails/{email_id}/events", "get")
    assert operations["cancel_email"] == ("/emails/{email_id}/cancel", "post")
    assert operations["reschedule_email"] == ("/emails/{email_id}", "patch")


def test_idempotency_header_matches_spec(spec: dict[str, Any]) -> None:
    """Ключ ставится ровно там, где его принимает API, и не длиннее его предела.

    У батча предел короче (252): бэкенд дописывает к ключу номер письма.
    """
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if not isinstance(operation, dict):
                continue
            headers = {
                parameter["name"]: parameter["schema"]
                for parameter in operation.get("parameters", [])
                if parameter.get("in") == "header"
            }
            if (path, method) not in IDEMPOTENT_ENDPOINTS:
                assert not headers, f"{method.upper()} {path}: заголовки {sorted(headers)}"
                continue
            assert set(headers) == {IDEMPOTENCY_KEY_HEADER}, f"{method.upper()} {path}"
            assert (
                headers[IDEMPOTENCY_KEY_HEADER]["maxLength"] == IDEMPOTENT_ENDPOINTS[(path, method)]
            ), f"{method.upper()} {path}: предел длины ключа разошёлся со спекой"


def test_spec_documents_rate_limit_and_replay(spec: dict[str, Any]) -> None:
    """SDK опирается на эти заголовки — они обязаны быть описаны в спеке."""
    send = spec["paths"]["/emails"]["post"]["responses"]

    assert "Retry-After" in send["429"]["headers"]
    assert "Idempotent-Replayed" in send["200"]["headers"]
    assert "X-Request-ID" in send["202"]["headers"]


def test_spec_declares_api_key_security(spec: dict[str, Any]) -> None:
    schemes = spec["components"]["securitySchemes"]

    assert schemes["ApiKeyBearer"]["type"] == "http"
    assert schemes["ApiKeyBearer"]["scheme"] == "bearer"
    assert spec["security"] == [{"ApiKeyBearer": []}]
    assert spec["servers"][0]["url"].endswith("/v1")
