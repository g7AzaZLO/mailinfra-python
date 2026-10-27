"""Ресурс писем: тело запроса, идемпотентность, чтение и батч."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from mailinfra import Attachment, EmailStatus, MailInfra, MailInfraError
from mailinfra.client import AsyncMailInfra
from mailinfra.resources.emails import MAX_BATCH_SIZE, build_send_payload

from .conftest import BASE_URL, EMAIL_ID, PROJECT_ID, SEND_RESPONSE


def test_payload_uses_api_aliases_and_drops_empty_values() -> None:
    payload = build_send_payload(
        from_email="hello@mail.example.ru",
        to="user@example.com",
        subject="Тема",
        html="<p>Привет</p>",
    )

    assert payload["from"] == "hello@mail.example.ru"
    assert payload["to"] == ["user@example.com"]
    assert "from_email" not in payload
    # Пустые cc/bcc/tags не отправляем — незачем шуметь в запросе.
    assert "cc" not in payload
    assert "text" not in payload


def test_payload_accepts_sequences_and_template_variables() -> None:
    payload = build_send_payload(
        from_email="hello@mail.example.ru",
        to=["a@example.com", "b@example.com"],
        cc="c@example.com",
        template="welcome",
        variables={"user_name": "Анна"},
        tags=["order"],
        reply_to="support@mail.example.ru",
    )

    assert payload["to"] == ["a@example.com", "b@example.com"]
    assert payload["cc"] == ["c@example.com"]
    assert payload["reply_to"] == ["support@mail.example.ru"]
    assert payload["template"] == "welcome"
    assert payload["variables"] == {"user_name": "Анна"}


def test_payload_encodes_attachment_bytes() -> None:
    payload = build_send_payload(
        from_email="hello@mail.example.ru",
        to="user@example.com",
        subject="Счёт",
        text="во вложении",
        attachments=[
            Attachment(filename="bill.pdf", content=b"%PDF-1.4", content_type="application/pdf")
        ],
    )

    attachment = payload["attachments"][0]
    assert attachment["filename"] == "bill.pdf"
    assert base64.b64decode(attachment["content_base64"]) == b"%PDF-1.4"
    assert attachment["content_type"] == "application/pdf"


def test_payload_validation_runs_before_network(client: MailInfra) -> None:
    """Лимиты спеки проверяются на клиенте: 51 получатель не уедет в сеть.

    Ошибка — `MailInfraError`, а не `pydantic.ValidationError`: README учит
    ловить первую, и голый `ValidationError` пролетал бы мимо такого except.
    Go и PHP отдают на этом же письме свой `invalid_request`.
    """
    with pytest.raises(MailInfraError) as excinfo:
        build_send_payload(
            from_email="hello@mail.example.ru",
            to=[f"user{index}@example.com" for index in range(51)],
            subject="Тема",
            text="Текст",
        )

    assert excinfo.value.code == "invalid_request"
    assert "to" in str(excinfo.value)
    assert any(item["field"] == "to" for item in excinfo.value.details["fields"])


def test_send_sets_idempotency_key_automatically(client: MailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/emails").mock(return_value=httpx.Response(202, json=SEND_RESPONSE))
        result = client.emails.send(
            from_email="hello@mail.example.ru",
            to="user@example.com",
            subject="Тема",
            text="Текст",
        )

    sent_key = route.calls[0].request.headers["Idempotency-Key"]
    assert result.idempotency_key == sent_key
    assert result.status is EmailStatus.QUEUED
    assert result.replayed is False


def test_send_honours_explicit_idempotency_key(client: MailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/emails").mock(return_value=httpx.Response(202, json=SEND_RESPONSE))
        client.emails.send(
            from_email="hello@mail.example.ru",
            to="user@example.com",
            subject="Тема",
            text="Текст",
            idempotency_key="order_123_welcome",
        )

    assert route.calls[0].request.headers["Idempotency-Key"] == "order_123_welcome"


def test_send_rejects_too_long_idempotency_key(client: MailInfra) -> None:
    # Именно MailInfraError: README учит ловить его, и ValueError пролетал бы
    # мимо такого except, роняя обработчик целиком.
    with pytest.raises(MailInfraError, match="Idempotency-Key") as excinfo:
        client.emails.send(
            from_email="hello@mail.example.ru",
            to="user@example.com",
            subject="Тема",
            text="Текст",
            idempotency_key="x" * 256,
        )

    assert excinfo.value.code == "invalid_idempotency_key"


@pytest.mark.parametrize("key", ["", "   "])
def test_send_rejects_empty_idempotency_key(client: MailInfra, key: str) -> None:
    """Пустой ключ бэкенд игнорирует, а запрос выглядел бы идемпотентным.

    Дедупликация в `email_service.send_email` стоит под
    `if payload.idempotency_key:`, куда пустая строка не проходит: повтор
    после 5xx создал бы второе письмо.
    """
    with pytest.raises(MailInfraError) as excinfo:
        client.emails.send(
            from_email="hello@mail.example.ru",
            to="user@example.com",
            subject="Тема",
            text="Текст",
            idempotency_key=key,
        )

    assert excinfo.value.code == "invalid_idempotency_key"


@pytest.mark.parametrize("key", ["заказ-42", "order-🎉", "order-42\r\nX-Injected: 1"])
def test_send_rejects_non_ascii_idempotency_key(client: MailInfra, key: str) -> None:
    """Значение заголовка уходит байтами, а бэкенд читает их как latin-1.

    httpx на таком значении падает `UnicodeEncodeError` — голым исключением
    мимо `except MailInfraError`, поэтому отбиваем его на клиенте. CR и LF
    отбиваются заодно: это инъекция заголовков.
    """
    with pytest.raises(MailInfraError) as excinfo:
        client.emails.send(
            from_email="hello@mail.example.ru",
            to="user@example.com",
            subject="Тема",
            text="Текст",
            idempotency_key=key,
        )

    assert excinfo.value.code == "invalid_idempotency_key"


def test_send_can_disable_auto_idempotency() -> None:
    with (
        MailInfra(
            api_key="mi_test_0123456789abcdef0123456789abcdef",
            base_url=BASE_URL,
            auto_idempotency=False,
        ) as client,
        respx.mock(base_url=BASE_URL) as mock,
    ):
        route = mock.post("/emails").mock(return_value=httpx.Response(202, json=SEND_RESPONSE))
        result = client.emails.send(
            from_email="hello@mail.example.ru",
            to="user@example.com",
            subject="Тема",
            text="Текст",
        )

    assert "Idempotency-Key" not in route.calls[0].request.headers
    assert result.idempotency_key is None


def test_send_marks_idempotent_replay(client: MailInfra) -> None:
    """`200` + `Idempotent-Replayed` означает «письмо уже было принято»."""
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/emails").mock(
            return_value=httpx.Response(
                200,
                json=SEND_RESPONSE,
                headers={"Idempotent-Replayed": "true", "X-Request-ID": "req-42"},
            )
        )
        result = client.emails.send(
            from_email="hello@mail.example.ru",
            to="user@example.com",
            subject="Тема",
            text="Текст",
            idempotency_key="order_123_welcome",
        )

    assert result.replayed is True
    assert result.request_id == "req-42"


def test_send_reports_skipped_recipients(client: MailInfra) -> None:
    payload = {
        "data": {
            "id": EMAIL_ID,
            "status": "QUEUED",
            "skipped_recipients": [{"email": "blocked@example.com", "reason": "suppressed"}],
        }
    }
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/emails").mock(return_value=httpx.Response(202, json=payload))
        result = client.emails.send(
            from_email="hello@mail.example.ru",
            to=["user@example.com", "blocked@example.com"],
            subject="Тема",
            text="Текст",
        )

    assert result.skipped_recipients == [{"email": "blocked@example.com", "reason": "suppressed"}]


def test_list_passes_filters_as_query(client: MailInfra) -> None:
    item = {
        "id": EMAIL_ID,
        "project_id": PROJECT_ID,
        "from_email": "hello@mail.example.ru",
        "to_emails": ["user@example.com"],
        "subject": "Тема",
        "status": "DELIVERED",
        "source": "API",
        "tags": ["order"],
        "created_at": "2026-05-11T20:00:00Z",
        "sent_at": "2026-05-11T20:00:01Z",
        "delivered_at": "2026-05-11T20:00:05Z",
    }
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get("/emails").mock(return_value=httpx.Response(200, json={"data": [item]}))
        emails = client.emails.list(status=EmailStatus.DELIVERED, tag="order", limit=10)

    query = route.calls[0].request.url.params
    assert query["status"] == "DELIVERED"
    assert query["tag"] == "order"
    assert query["limit"] == "10"
    assert emails[0].status is EmailStatus.DELIVERED
    # Строковое сравнение тоже должно работать: enum'ы — подклассы str.
    assert emails[0].status == "DELIVERED"


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 5, 11, 20, 0, 0),
        datetime(2026, 5, 11, 20, 0, 0, tzinfo=timezone(timedelta(hours=3))),
    ],
    ids=["naive", "aware"],
)
def test_list_sends_datetime_filters_with_timezone(client: MailInfra, moment: datetime) -> None:
    """Без смещения API читал локальное время как UTC и съезжал на часовой пояс.

    Наивный `datetime` трактуем как локальный — ровно так его создаёт
    `datetime.now()`; Node всегда шлёт `toISOString()`, то есть UTC.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get("/emails").mock(return_value=httpx.Response(200, json={"data": []}))
        client.emails.list(from_date=moment)

    sent = route.calls[0].request.url.params["from"]
    parsed = datetime.fromisoformat(sent)
    assert parsed.tzinfo is not None
    assert parsed == moment.astimezone(timezone.utc)


def test_get_and_events_parse_models(client: MailInfra) -> None:
    detail = {
        "data": {
            "id": EMAIL_ID,
            "project_id": PROJECT_ID,
            "from_email": "hello@mail.example.ru",
            "from_name": None,
            "to_emails": ["user@example.com"],
            "cc_emails": [],
            "bcc_emails": [],
            "reply_to": [],
            "subject": "Тема",
            "status": "DELIVERED",
            "source": "API",
            "tags": [],
            "html_body": "<p>Привет</p>",
            "text_body": "Привет",
            "headers": {},
            "created_at": "2026-05-11T20:00:00Z",
            "sent_at": "2026-05-11T20:00:01Z",
            "delivered_at": "2026-05-11T20:00:05Z",
            "failed_at": None,
        }
    }
    events = {
        "data": [
            {
                "id": "018e1b7a-3333-7000-cccc-333333333333",
                "type": "delivered",
                "data": {"mx": "gmail-smtp-in.l.google.com"},
                "timestamp": "2026-05-11T20:00:05Z",
            }
        ]
    }
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get(f"/emails/{EMAIL_ID}").mock(return_value=httpx.Response(200, json=detail))
        mock.get(f"/emails/{EMAIL_ID}/events").mock(return_value=httpx.Response(200, json=events))

        email = client.emails.get(EMAIL_ID)
        history = client.emails.events(EMAIL_ID)

    assert email.subject == "Тема"
    assert email.html_body == "<p>Привет</p>"
    assert history[0].type == "delivered"
    assert history[0].data["mx"] == "gmail-smtp-in.l.google.com"


def test_batch_reports_per_item_errors(client: MailInfra) -> None:
    """Батч возвращает 202 даже при частичном отказе — SDK не должен это скрывать."""
    payload = {
        "data": {
            "results": [
                {"index": 0, "id": EMAIL_ID, "status": "QUEUED", "skipped_recipients": []},
                {
                    "index": 1,
                    "error": {
                        "status_code": 400,
                        "code": "domain_not_verified",
                        "message": "Домен ещё не подтверждён.",
                        "details": {},
                    },
                },
            ],
            "accepted_count": 1,
            "failed_count": 1,
        }
    }
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/emails/batch").mock(return_value=httpx.Response(202, json=payload))
        result = client.emails.send_batch(
            [
                {
                    "from_email": "hello@mail.example.ru",
                    "to": "user@example.com",
                    "subject": "Тема",
                    "text": "Текст",
                },
                {
                    "from_email": "hello@unverified.example.ru",
                    "to": "user@example.com",
                    "subject": "Тема",
                    "text": "Текст",
                },
            ]
        )

    body = route.calls[0].request.read().decode("utf-8")
    assert '"from"' in body
    assert result.failed_count == 1
    assert result.results[1].error is not None
    assert result.results[1].error.code == "domain_not_verified"
    # Батч идёт без заголовка идемпотентности: API его не принимает.
    assert "Idempotency-Key" not in route.calls[0].request.headers


def test_batch_rejects_empty_list(client: MailInfra) -> None:
    with pytest.raises(MailInfraError, match="Пустой батч"):
        client.emails.send_batch([])


def test_batch_rejects_oversized_list(client: MailInfra) -> None:
    """101 письмо с вложениями уехало бы целиком только чтобы вернуться с 422."""
    item = {
        "from_email": "hello@mail.example.ru",
        "to": "user@example.com",
        "subject": "Тема",
        "text": "Текст",
    }
    with pytest.raises(MailInfraError) as excinfo:
        client.emails.send_batch([item] * (MAX_BATCH_SIZE + 1))

    assert excinfo.value.code == "batch_too_large"


def test_batch_rejects_per_item_idempotency_key(client: MailInfra) -> None:
    """README описывает элементы батча как параметры `send()`, а тот берёт ключ.

    Эндпоинт батча заголовок не принимает вовсе; раньше это был голый
    TypeError мимо всех документированных except-ов.
    """
    with pytest.raises(MailInfraError) as excinfo:
        client.emails.send_batch(
            [
                {
                    "from_email": "hello@mail.example.ru",
                    "to": "user@example.com",
                    "subject": "Тема",
                    "text": "Текст",
                    "idempotency_key": "k1",
                }
            ]
        )

    assert excinfo.value.code == "invalid_batch_item"


def test_batch_reports_unknown_field_as_mailinfra_error(client: MailInfra) -> None:
    with pytest.raises(MailInfraError) as excinfo:
        client.emails.send_batch(
            [
                {
                    "from_email": "hello@mail.example.ru",
                    "to": "user@example.com",
                    "subject": "Тема",
                    "text": "Текст",
                    "nonsense": 1,
                }
            ]
        )

    assert excinfo.value.code == "invalid_batch_item"


async def test_async_client_sends_and_reads(async_client: AsyncMailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/emails").mock(return_value=httpx.Response(202, json=SEND_RESPONSE))
        result = await async_client.emails.send(
            from_email="hello@mail.example.ru",
            to="user@example.com",
            subject="Тема",
            text="Текст",
        )

    assert str(result.id) == EMAIL_ID
    assert result.idempotency_key is not None


async def test_async_client_retries_on_503(async_client: AsyncMailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/emails").mock(
            side_effect=[httpx.Response(503), httpx.Response(202, json=SEND_RESPONSE)]
        )
        await async_client.emails.send(
            from_email="hello@mail.example.ru",
            to="user@example.com",
            subject="Тема",
            text="Текст",
        )

    keys = {call.request.headers["Idempotency-Key"] for call in route.calls}
    assert route.call_count == 2
    assert len(keys) == 1


def test_email_id_is_escaped_in_path(client: MailInfra) -> None:
    """Идентификатор приходит снаружи: со слешем он не должен менять эндпоинт."""
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get(url__regex=r".*").mock(return_value=httpx.Response(200, json={"data": []}))
        client.emails.events("../../admin/emails")

    # httpx показывает `.path` уже раскодированным, поэтому сверяем сырой URL.
    assert str(route.calls[0].request.url).endswith("/emails/..%2F..%2Fadmin%2Femails/events")


def _detail_with_status(status: str) -> dict[str, object]:
    return {
        "data": {
            "id": EMAIL_ID,
            "project_id": PROJECT_ID,
            "from_email": "hello@mail.example.ru",
            "to_emails": ["user@example.com"],
            "cc_emails": [],
            "bcc_emails": [],
            "reply_to": [],
            "subject": "Тема",
            "status": status,
            "source": "API",
            "tags": [],
            "headers": {},
            "created_at": "2026-05-11T20:00:00Z",
        }
    }


def test_unexpected_enum_in_response_is_mailinfra_error(client: MailInfra) -> None:
    """Аудит: S1-22 — новое значение enum от бэкенда даёт MailInfraError.

    Раньше вызов падал голым `pydantic.ValidationError`, и `except
    MailInfraError` из README его не ловил.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get(f"/emails/{EMAIL_ID}").mock(
            return_value=httpx.Response(200, json=_detail_with_status("ARCHIVED_IN_FUTURE"))
        )
        with pytest.raises(MailInfraError) as caught:
            client.emails.get(EMAIL_ID)

    assert caught.value.code == "malformed_response"


async def test_unexpected_enum_in_response_is_mailinfra_error_async(
    async_client: AsyncMailInfra,
) -> None:
    """Аудит: S1-22 — то же для асинхронного клиента."""
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get(f"/emails/{EMAIL_ID}").mock(
            return_value=httpx.Response(200, json=_detail_with_status("ARCHIVED_IN_FUTURE"))
        )
        with pytest.raises(MailInfraError) as caught:
            await async_client.emails.get(EMAIL_ID)

    assert caught.value.code == "malformed_response"


def test_unparsed_send_response_keeps_request_id_and_idempotency_key(client: MailInfra) -> None:
    """Ревью блока 11, п. 3 (S1-22): ответ 202 не разобран — письмо уже принято.

    В ошибке есть status_code, request_id и ключ идемпотентности: повтор
    send() с этим ключом не создаст дубль, а request_id нужен поддержке.
    """
    body = {"data": {**SEND_RESPONSE["data"], "status": "QUEUED_IN_FUTURE"}}
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/emails").mock(
            return_value=httpx.Response(202, json=body, headers={"X-Request-ID": "req-7"})
        )
        with pytest.raises(MailInfraError) as caught:
            client.emails.send(
                from_email="hello@mail.example.ru",
                to="user@example.com",
                subject="Тема",
                text="Текст",
                idempotency_key="order_7",
            )

    assert caught.value.code == "malformed_response"
    assert caught.value.status_code == 202
    assert caught.value.request_id == "req-7"
    assert caught.value.details["idempotency_key"] == "order_7"


def test_list_with_non_list_data_is_mailinfra_error(client: MailInfra) -> None:
    """Ревью блока 11, п. 3: `data: null` в списке — malformed_response, а не TypeError."""
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/emails").mock(return_value=httpx.Response(200, json={"data": None}))
        mock.get(f"/emails/{EMAIL_ID}/events").mock(
            return_value=httpx.Response(200, json={"data": None})
        )
        with pytest.raises(MailInfraError) as listed:
            client.emails.list()
        with pytest.raises(MailInfraError) as events:
            client.emails.events(EMAIL_ID)

    assert listed.value.code == "malformed_response"
    assert events.value.code == "malformed_response"


async def test_list_with_non_list_data_is_mailinfra_error_async(
    async_client: AsyncMailInfra,
) -> None:
    """Ревью блока 11, п. 3 — то же для асинхронного клиента."""
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/emails").mock(return_value=httpx.Response(200, json={"data": None}))
        with pytest.raises(MailInfraError) as listed:
            await async_client.emails.list()

    assert listed.value.code == "malformed_response"


def test_unparsed_batch_and_list_keep_request_id(client: MailInfra) -> None:
    """Ревью блока 13, п. 8: у батча нет идемпотентности — неразобранный ответ
    с уже принятыми письмами должен нести request_id для поддержки; у списка
    — тоже."""
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/emails/batch").mock(
            return_value=httpx.Response(
                200, json={"data": {"unexpected": True}}, headers={"X-Request-ID": "req-b"}
            )
        )
        mock.get("/emails").mock(
            return_value=httpx.Response(200, json={"data": None}, headers={"X-Request-ID": "req-l"})
        )
        with pytest.raises(MailInfraError) as batch:
            client.emails.send_batch(
                [
                    {
                        "from_email": "a@mail.example.ru",
                        "to": "b@example.com",
                        "subject": "s",
                        "text": "t",
                    }
                ]
            )
        with pytest.raises(MailInfraError) as listed:
            client.emails.list()

    assert (batch.value.code, batch.value.status_code, batch.value.request_id) == (
        "malformed_response",
        200,
        "req-b",
    )
    assert (listed.value.status_code, listed.value.request_id) == (200, "req-l")
