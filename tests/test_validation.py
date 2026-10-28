"""Клиентская валидация новых полей письма: metadata, send_at, ttl.

Лимиты `metadata` и требование часового пояса у `send_at` в спеке машинно не
записаны (только в описании), поэтому генератор их не видит — их держит
`mailinfra._validation`. Источник истины — `normalize_metadata` и
`resolve_send_at` бэкенда; тесты проверяют и нарушение, и саму границу, чтобы
SDK не отклонял того, что API принял бы.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from mailinfra import MailInfraError
from mailinfra._validation import (
    MAX_METADATA_BYTES,
    MAX_METADATA_KEY_LENGTH,
    MAX_METADATA_KEYS,
    MAX_METADATA_VALUE_LENGTH,
)
from mailinfra.resources.emails import build_send_payload

MOSCOW = timezone(timedelta(hours=3))


def _payload(**fields: Any) -> dict[str, Any]:
    """Собирает минимальное письмо с дополнительными полями.

    Args:
        **fields: Поля сверх отправителя, получателя, темы и текста.

    Returns:
        JSON-тело запроса.
    """
    return build_send_payload(
        from_email="hello@mail.example.ru",
        to="user@example.com",
        subject="Тема",
        text="Текст",
        **fields,
    )


def _rejected_fields(**fields: Any) -> list[str]:
    """Возвращает поля, на которые указала клиентская валидация.

    Args:
        **fields: Поля письма, которые должны быть отклонены.

    Returns:
        Имена полей из `details["fields"]`.
    """
    with pytest.raises(MailInfraError) as excinfo:
        _payload(**fields)

    assert excinfo.value.code == "invalid_request"
    return [item["field"] for item in excinfo.value.details["fields"]]


def test_metadata_is_sent_as_is() -> None:
    payload = _payload(metadata={"order_id": "4242", "user.id": "u-1", "a_b-c": ""})

    assert payload["metadata"] == {"order_id": "4242", "user.id": "u-1", "a_b-c": ""}


def test_empty_metadata_is_not_sent() -> None:
    assert "metadata" not in _payload(metadata={})


def test_metadata_accepts_key_and_value_limits() -> None:
    metadata = {"k" * MAX_METADATA_KEY_LENGTH: "v" * MAX_METADATA_VALUE_LENGTH}

    assert _payload(metadata=metadata)["metadata"] == metadata


@pytest.mark.parametrize(
    "metadata",
    [
        {f"key{index}": "v" for index in range(MAX_METADATA_KEYS + 1)},
        {"k" * (MAX_METADATA_KEY_LENGTH + 1): "v"},
        {"": "v"},
        {"order id": "v"},
        {"заказ": "v"},
        {"order\n": "v"},
        {"order_id": "v" * (MAX_METADATA_VALUE_LENGTH + 1)},
        {"order_id": "a\x00b"},
        {"order_id": 4242},
    ],
    ids=[
        "too_many_keys",
        "key_too_long",
        "empty_key",
        "key_with_space",
        "cyrillic_key",
        "key_with_trailing_newline",
        "value_too_long",
        "value_with_nul",
        "value_not_string",
    ],
)
def test_metadata_limits_are_checked_before_network(metadata: dict[str, Any]) -> None:
    assert _rejected_fields(metadata=metadata)[0].startswith("metadata")


def test_metadata_size_is_counted_like_backend() -> None:
    """4096 байт — по `json.dumps` с разделителями по умолчанию, в UTF-8.

    Кириллица весит два байта на символ: по символам такой объект вдвое
    меньше лимита, а в байтах — больше.
    """
    metadata = {f"key{index}": "я" * 420 for index in range(5)}
    serialized = json.dumps(metadata, ensure_ascii=False)
    assert len(serialized) < MAX_METADATA_BYTES < len(serialized.encode("utf-8"))

    assert _rejected_fields(metadata=metadata) == ["metadata"]


def test_metadata_exactly_at_size_limit_is_accepted() -> None:
    metadata = {f"key{index}": "v" * 450 for index in range(8)}
    overhead = len(json.dumps({**metadata, "last": ""}, ensure_ascii=False).encode("utf-8"))
    metadata["last"] = "v" * (MAX_METADATA_BYTES - overhead)

    assert len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) == MAX_METADATA_BYTES
    assert _payload(metadata=metadata)["metadata"] == metadata

    metadata["last"] += "v"
    assert _rejected_fields(metadata=metadata) == ["metadata"]


@pytest.mark.parametrize(
    ("send_at", "expected"),
    [
        (datetime(2026, 11, 1, 9, 0, tzinfo=MOSCOW), "2026-11-01T09:00:00+03:00"),
        (datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc), "2026-11-01T06:00:00Z"),
        ("2026-11-01T09:00:00+03:00", "2026-11-01T09:00:00+03:00"),
        ("2026-11-01T06:00:00Z", "2026-11-01T06:00:00Z"),
    ],
    ids=["aware_datetime", "utc_datetime", "string_with_offset", "string_with_z"],
)
def test_send_at_with_timezone_is_sent_as_iso8601(send_at: datetime | str, expected: str) -> None:
    sent = _payload(send_at=send_at)["send_at"]

    assert sent == expected
    assert datetime.fromisoformat(sent.replace("Z", "+00:00")).utcoffset() is not None


@pytest.mark.parametrize(
    "send_at",
    [datetime(2026, 11, 1, 9, 0), "2026-11-01T09:00:00", "завтра в девять"],
    ids=["naive_datetime", "string_without_offset", "garbage"],
)
def test_send_at_without_timezone_is_rejected(send_at: datetime | str) -> None:
    """Наивное время неоднозначно: письмо ушло бы на часовой пояс раньше или позже.

    API отклонил бы его с 422, поэтому SDK отказывает до сетевого вызова, а не
    додумывает пояс хоста (как делает `list(from_date=…)`, где ошибка безвредна).
    """
    assert _rejected_fields(send_at=send_at) == ["send_at"]


def test_send_at_is_not_checked_against_horizon() -> None:
    """Горизонт в 7 дней считается по часам сервера — решает он, а не клиент."""
    far = datetime.now(timezone.utc) + timedelta(days=30)

    assert "send_at" in _payload(send_at=far)


@pytest.mark.parametrize("ttl", [60, 3600, 86400])
def test_ttl_within_range_is_sent(ttl: int) -> None:
    assert _payload(ttl=ttl)["ttl"] == ttl


@pytest.mark.parametrize("ttl", [0, 59, 86401])
def test_ttl_out_of_range_is_rejected(ttl: int) -> None:
    assert _rejected_fields(ttl=ttl) == ["ttl"]


def test_all_violations_come_in_one_error() -> None:
    """Лимиты спеки и лимиты SDK приходят одним списком, а не по полю за вызов."""
    fields = _rejected_fields(
        metadata={"bad key": "v"},
        send_at=datetime(2026, 11, 1, 9, 0),
        ttl=10,
    )

    assert sorted(fields) == ["metadata", "send_at", "ttl"]
