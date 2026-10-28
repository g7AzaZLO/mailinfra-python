"""Курсорная пагинация `GET /v1/emails`: страница `list()` и обход `list_all()`."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from mailinfra import EmailListResponse, MailInfra, MailInfraError
from mailinfra.client import AsyncMailInfra

from .conftest import BASE_URL, list_item, list_page

FIRST_ID = "018e1b7a-1111-7000-aaaa-000000000001"
SECOND_ID = "018e1b7a-1111-7000-aaaa-000000000002"
THIRD_ID = "018e1b7a-1111-7000-aaaa-000000000003"


def _three_pages() -> list[httpx.Response]:
    """Три страницы по одному письму, связанные курсорами.

    Returns:
        Ответы API в порядке запросов.
    """
    return [
        httpx.Response(200, json=list_page([list_item(FIRST_ID)], next_cursor="c1")),
        httpx.Response(200, json=list_page([list_item(SECOND_ID)], next_cursor="c2")),
        httpx.Response(200, json=list_page([list_item(THIRD_ID)])),
    ]


def _cursors(route: respx.Route) -> list[str | None]:
    """Курсоры, с которыми SDK запрашивал страницы.

    Args:
        route: Маршрут respx для `GET /emails`.

    Returns:
        Значение `cursor` каждого запроса; `None` — без курсора.
    """
    return [call.request.url.params.get("cursor") for call in route.calls]


def test_list_returns_page_with_cursor(client: MailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get("/emails").mock(
            return_value=httpx.Response(
                200, json=list_page([list_item(FIRST_ID)], next_cursor="c1")
            )
        )
        page = client.emails.list(limit=1, cursor="c0")

    assert isinstance(page, EmailListResponse)
    assert [str(email.id) for email in page.data] == [FIRST_ID]
    assert page.has_more is True
    assert page.next_cursor == "c1"
    assert route.calls[0].request.url.params["cursor"] == "c0"


def test_list_last_page_has_no_cursor(client: MailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/emails").mock(return_value=httpx.Response(200, json=list_page([])))
        page = client.emails.list()

    assert page.data == []
    assert page.has_more is False
    assert page.next_cursor is None


def test_list_without_has_more_is_malformed(client: MailInfra) -> None:
    """Голый список 0.1.x — не по контракту: без `has_more` не понять, конец ли это."""
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/emails").mock(
            return_value=httpx.Response(
                200, json={"data": [list_item()]}, headers={"X-Request-ID": "req-l"}
            )
        )
        with pytest.raises(MailInfraError) as caught:
            client.emails.list()

    assert caught.value.code == "malformed_response"
    assert caught.value.request_id == "req-l"


def test_list_all_follows_cursors_with_the_same_filters(client: MailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get("/emails").mock(side_effect=_three_pages())
        emails = list(client.emails.list_all(status="DELIVERED", tag="order", limit=1))

    assert [str(email.id) for email in emails] == [FIRST_ID, SECOND_ID, THIRD_ID]
    assert _cursors(route) == [None, "c1", "c2"]
    for call in route.calls:
        params = call.request.url.params
        assert (params["status"], params["tag"], params["limit"]) == ("DELIVERED", "order", "1")


def test_list_all_can_resume_from_cursor(client: MailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get("/emails").mock(side_effect=_three_pages()[1:])
        emails = list(client.emails.list_all(cursor="c1"))

    assert [str(email.id) for email in emails] == [SECOND_ID, THIRD_ID]
    assert _cursors(route) == ["c1", "c2"]


def test_list_all_is_lazy(client: MailInfra) -> None:
    """Прерванный обход не запрашивает страницы, которые никто не прочтёт."""
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get("/emails").mock(side_effect=_three_pages())
        iterator = client.emails.list_all()
        assert route.call_count == 0

        first = next(iterator)

    assert str(first.id) == FIRST_ID
    assert route.call_count == 1


def test_list_all_skips_empty_pages_until_has_more_is_false(client: MailInfra) -> None:
    """Пустая страница с `has_more=true` — не конец: фильтр мог отсеять её целиком."""
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get("/emails").mock(
            side_effect=[
                httpx.Response(200, json=list_page([], next_cursor="c1")),
                httpx.Response(200, json=list_page([list_item(FIRST_ID)])),
            ]
        )
        emails = list(client.emails.list_all())

    assert [str(email.id) for email in emails] == [FIRST_ID]
    assert route.call_count == 2


def test_list_all_rejects_has_more_without_cursor(client: MailInfra) -> None:
    """Без курсора следующую страницу не запросить, а молча оборвать — потерять письма."""
    body: dict[str, Any] = {"data": [list_item()], "has_more": True, "next_cursor": None}
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get("/emails").mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(MailInfraError) as caught:
            list(client.emails.list_all())

    assert caught.value.code == "malformed_response"
    assert route.call_count == 1


async def test_async_list_returns_page(async_client: AsyncMailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get("/emails").mock(
            return_value=httpx.Response(
                200, json=list_page([list_item(FIRST_ID)], next_cursor="c1")
            )
        )
        page = await async_client.emails.list(cursor="c0")

    assert page.next_cursor == "c1"
    assert route.calls[0].request.url.params["cursor"] == "c0"


async def test_async_list_all_follows_cursors(async_client: AsyncMailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get("/emails").mock(side_effect=_three_pages())
        emails = [email async for email in async_client.emails.list_all(limit=1)]

    assert [str(email.id) for email in emails] == [FIRST_ID, SECOND_ID, THIRD_ID]
    assert _cursors(route) == [None, "c1", "c2"]
