"""Отложенная отправка: `send(send_at=…)`, `cancel()` и `reschedule()`."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from mailinfra import (
    ConflictError,
    EmailNotScheduledError,
    EmailScheduleData,
    EmailStatus,
    MailInfra,
    MailInfraError,
    SendAtTooFarError,
)
from mailinfra.client import AsyncMailInfra

from .conftest import BASE_URL, EMAIL_ID

MOSCOW = timezone(timedelta(hours=3))
SEND_AT = datetime(2026, 11, 1, 9, 0, tzinfo=MOSCOW)

SCHEDULED = {
    "data": {"id": EMAIL_ID, "status": "SCHEDULED", "scheduled_at": "2026-11-01T06:00:00Z"}
}
CANCELED = {"data": {"id": EMAIL_ID, "status": "CANCELED", "scheduled_at": None}}


def _error(code: str, details: dict[str, object] | None = None) -> dict[str, object]:
    """Конверт ошибки API с кодом, продублированным в `details.code`.

    Args:
        code: Машинный код.
        details: Дополнительные детали.

    Returns:
        Тело ответа об ошибке.
    """
    return {
        "error": {"code": code, "message": "Ошибка", "details": {"code": code, **(details or {})}},
        "request_id": "req-test",
    }


def test_send_schedules_email_with_metadata_and_ttl(client: MailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/emails").mock(
            return_value=httpx.Response(202, json={"data": {"id": EMAIL_ID, "status": "SCHEDULED"}})
        )
        result = client.emails.send(
            from_email="hello@mail.example.ru",
            to="user@example.com",
            subject="Напоминание",
            text="Оплатите заказ",
            metadata={"order_id": "4242"},
            send_at=SEND_AT,
            ttl=3600,
        )

    body = json.loads(route.calls[0].request.read())
    assert body["send_at"] == "2026-11-01T09:00:00+03:00"
    assert body["ttl"] == 3600
    assert body["metadata"] == {"order_id": "4242"}
    assert result.status is EmailStatus.SCHEDULED


def test_naive_send_at_never_reaches_network(client: MailInfra) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        route = mock.post("/emails").mock(return_value=httpx.Response(202))
        with pytest.raises(MailInfraError) as excinfo:
            client.emails.send(
                from_email="hello@mail.example.ru",
                to="user@example.com",
                subject="Тема",
                text="Текст",
                send_at=datetime(2026, 11, 1, 9, 0),
            )

    assert excinfo.value.code == "invalid_request"
    assert not route.called


def test_cancel_returns_schedule_state(client: MailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post(f"/emails/{EMAIL_ID}/cancel").mock(
            return_value=httpx.Response(200, json=CANCELED)
        )
        state = client.emails.cancel(EMAIL_ID)

    assert isinstance(state, EmailScheduleData)
    assert state.status is EmailStatus.CANCELED
    assert state.scheduled_at is None
    assert route.calls[0].request.content == b""


def test_cancel_of_sent_email_raises_specific_conflict(client: MailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post(f"/emails/{EMAIL_ID}/cancel").mock(
            return_value=httpx.Response(409, json=_error("email_not_scheduled", {"status": "SENT"}))
        )
        with pytest.raises(EmailNotScheduledError) as excinfo:
            client.emails.cancel(EMAIL_ID)

    # Конфликт не повторяется: состояние письма от повтора не изменится.
    assert route.call_count == 1
    assert isinstance(excinfo.value, ConflictError)
    assert excinfo.value.details["status"] == "SENT"


def test_cancel_escapes_email_id(client: MailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post(url__regex=r".*").mock(return_value=httpx.Response(200, json=CANCELED))
        client.emails.cancel("../batch")

    assert str(route.calls[0].request.url).endswith("/emails/..%2Fbatch/cancel")


@pytest.mark.parametrize(
    ("send_at", "expected"),
    [
        (SEND_AT, "2026-11-01T09:00:00+03:00"),
        ("2026-11-01T09:00:00+03:00", "2026-11-01T09:00:00+03:00"),
    ],
    ids=["datetime", "string"],
)
def test_reschedule_patches_send_at(
    client: MailInfra, send_at: datetime | str, expected: str
) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.patch(f"/emails/{EMAIL_ID}").mock(
            return_value=httpx.Response(200, json=SCHEDULED)
        )
        state = client.emails.reschedule(EMAIL_ID, send_at=send_at)

    assert json.loads(route.calls[0].request.read()) == {"send_at": expected}
    assert state.status is EmailStatus.SCHEDULED
    assert state.scheduled_at == datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("send_at", [datetime(2026, 11, 1, 9, 0), "2026-11-01T09:00:00"])
def test_reschedule_rejects_naive_send_at(client: MailInfra, send_at: datetime | str) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        route = mock.patch(f"/emails/{EMAIL_ID}").mock(return_value=httpx.Response(200))
        with pytest.raises(MailInfraError) as excinfo:
            client.emails.reschedule(EMAIL_ID, send_at=send_at)

    assert excinfo.value.code == "invalid_request"
    assert excinfo.value.details["fields"][0]["field"] == "send_at"
    assert not route.called


def test_reschedule_too_far_raises_specific_error(client: MailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        mock.patch(f"/emails/{EMAIL_ID}").mock(
            return_value=httpx.Response(400, json=_error("send_at_too_far", {"max_days": 7}))
        )
        with pytest.raises(SendAtTooFarError) as excinfo:
            client.emails.reschedule(EMAIL_ID, send_at=SEND_AT + timedelta(days=30))

    assert excinfo.value.details["max_days"] == 7


async def test_async_cancel_and_reschedule(async_client: AsyncMailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post(f"/emails/{EMAIL_ID}/cancel").mock(
            return_value=httpx.Response(200, json=CANCELED)
        )
        route = mock.patch(f"/emails/{EMAIL_ID}").mock(
            return_value=httpx.Response(200, json=SCHEDULED)
        )
        canceled = await async_client.emails.cancel(EMAIL_ID)
        rescheduled = await async_client.emails.reschedule(EMAIL_ID, send_at=SEND_AT)

    assert canceled.status is EmailStatus.CANCELED
    assert rescheduled.status is EmailStatus.SCHEDULED
    assert json.loads(route.calls[0].request.read()) == {"send_at": "2026-11-01T09:00:00+03:00"}


async def test_async_send_forwards_new_fields(async_client: AsyncMailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/emails").mock(
            return_value=httpx.Response(202, json={"data": {"id": EMAIL_ID, "status": "SCHEDULED"}})
        )
        await async_client.emails.send(
            from_email="hello@mail.example.ru",
            to="user@example.com",
            subject="Тема",
            text="Текст",
            metadata={"order_id": "4242"},
            send_at=SEND_AT,
            ttl=60,
        )

    body = json.loads(route.calls[0].request.read())
    assert (body["metadata"], body["send_at"], body["ttl"]) == (
        {"order_id": "4242"},
        "2026-11-01T09:00:00+03:00",
        60,
    )
