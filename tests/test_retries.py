"""Повторы, backoff и `Retry-After`.

Это главная причина существования SDK: вызывающий не должен сам писать
retry-цикл и разбирать заголовки лимитов. Поэтому проверяется и то, что
повтор происходит, и то, что он НЕ происходит там, где повторять опасно или
бессмысленно.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from mailinfra import (
    APIConnectionError,
    APITimeoutError,
    MailInfra,
    RateLimitError,
    RetryPolicy,
    ServerError,
    UnprocessableError,
)

from .conftest import BASE_URL, EMAIL_ID, SEND_RESPONSE, TEST_API_KEY

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

BATCH_ITEM = {
    "from_email": "hello@mail.example.ru",
    "to": "user@example.com",
    "subject": "Тема",
    "text": "Текст",
}
BATCH_RESPONSE = {
    "data": {
        "results": [{"index": 0, "id": EMAIL_ID, "status": "QUEUED"}],
        "accepted_count": 1,
        "failed_count": 0,
    }
}


def _client_without_auto_key() -> MailInfra:
    """Клиент без авто-ключа: отправка и батч у него неидемпотентны.

    Returns:
        Клиент с двумя повторами и нулевым backoff.
    """
    return MailInfra(
        api_key=TEST_API_KEY,
        base_url=BASE_URL,
        auto_idempotency=False,
        retry_policy=RetryPolicy(max_retries=2, initial_backoff=0.0),
    )


def _send(client: MailInfra) -> object:
    """Отправляет минимальное письмо.

    Args:
        client: Клиент SDK.

    Returns:
        Результат отправки.
    """
    return client.emails.send(
        from_email="hello@mail.example.ru",
        to="user@example.com",
        subject="Тема",
        text="Текст",
    )


def test_retries_on_503_then_succeeds(client: MailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/emails").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(202, json=SEND_RESPONSE),
            ]
        )
        result = _send(client)

    assert route.call_count == 2
    assert str(result.id) == SEND_RESPONSE["data"]["id"]


def test_retries_reuse_the_same_idempotency_key(client: MailInfra) -> None:
    """Ключ идемпотентности обязан быть одним на все попытки одного вызова.

    Иначе повтор после `503` создал бы второе письмо — ровно та ошибка,
    от которой SDK должен защищать.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/emails").mock(
            side_effect=[
                httpx.Response(500),
                httpx.Response(500),
                httpx.Response(202, json=SEND_RESPONSE),
            ]
        )
        result = _send(client)

    keys = {call.request.headers["Idempotency-Key"] for call in route.calls}
    assert len(keys) == 1
    assert result.idempotency_key == keys.pop()


def test_respects_retry_after_header(client: MailInfra, no_sleep: list[float]) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/emails").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "3"}, json={}),
                httpx.Response(202, json=SEND_RESPONSE),
            ]
        )
        _send(client)

    # Пауза взята из заголовка как есть, без jitter: сервер знает про свои
    # окна лимитов больше клиента.
    assert no_sleep == [3.0]


def test_gives_up_when_retry_after_is_too_long(client: MailInfra) -> None:
    """Слишком долгий `Retry-After` не должен подвешивать вызывающего."""
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/emails").mock(
            return_value=httpx.Response(
                429,
                headers={"Retry-After": "600"},
                json={
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": "Превышен лимит.",
                        "details": {},
                    },
                    "request_id": "req-1",
                },
            )
        )
        with pytest.raises(RateLimitError) as excinfo:
            _send(client)

    assert route.call_count == 1
    assert excinfo.value.retry_after == 600
    assert excinfo.value.request_id == "req-1"


def test_does_not_retry_validation_error(client: MailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/emails").mock(
            return_value=httpx.Response(
                422,
                json={
                    "error": {
                        "code": "request_validation_failed",
                        "message": "Проверьте заполненные поля.",
                        "details": {
                            "fields": [{"field": "to", "message": "укажите корректный email"}]
                        },
                    },
                    "request_id": "req-2",
                },
            )
        )
        with pytest.raises(UnprocessableError) as excinfo:
            _send(client)

    assert route.call_count == 1
    assert excinfo.value.fields[0]["field"] == "to"


def test_batch_without_key_is_not_retried_on_server_error(no_sleep: list[float]) -> None:
    """Батч без ключа неидемпотентен: повтор после `5xx` мог бы отправить часть писем дважды."""
    with _client_without_auto_key() as client, respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/emails/batch").mock(return_value=httpx.Response(500))
        with pytest.raises(ServerError):
            client.emails.send_batch([BATCH_ITEM])

    assert route.call_count == 1
    assert "Idempotency-Key" not in route.calls[0].request.headers


def test_batch_with_key_is_retried_with_the_same_key(client: MailInfra) -> None:
    """С ключом батч идемпотентен: бэкенд выводит из него ключ каждого письма.

    Повтор после `503` не дублирует уже принятые письма — но только если ключ
    тот же, что у первой попытки.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/emails/batch").mock(
            side_effect=[
                httpx.Response(503),
                httpx.ReadTimeout("slow"),
                httpx.Response(202, json=BATCH_RESPONSE),
            ]
        )
        result = client.emails.send_batch([BATCH_ITEM])

    keys = {call.request.headers["Idempotency-Key"] for call in route.calls}
    assert route.call_count == 3
    assert len(keys) == 1
    assert result.idempotency_key == keys.pop()
    assert result.replayed is False


def test_batch_is_retried_on_rate_limit(client: MailInfra) -> None:
    """`429` для батча безопасен: лимитёр отказывает до приёма писем."""
    batch_response = {
        "data": {
            "results": [{"index": 0, "id": SEND_RESPONSE["data"]["id"], "status": "QUEUED"}],
            "accepted_count": 1,
            "failed_count": 0,
        }
    }
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/emails/batch").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "1"}, json={}),
                httpx.Response(202, json=batch_response),
            ]
        )
        result = client.emails.send_batch(
            [
                {
                    "from_email": "hello@mail.example.ru",
                    "to": "user@example.com",
                    "subject": "Тема",
                    "text": "Текст",
                }
            ]
        )

    assert route.call_count == 2
    assert result.accepted_count == 1


def test_retries_network_errors_then_raises_connection_error(client: MailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/emails").mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(APIConnectionError):
            _send(client)

    # Первая попытка плюс два повтора.
    assert route.call_count == 3


def test_retries_timeouts_then_raises_timeout_error(client: MailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/emails").mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(APITimeoutError):
            _send(client)

    assert route.call_count == 3


def test_backoff_grows_exponentially(no_sleep: list[float]) -> None:
    policy = RetryPolicy(max_retries=2, initial_backoff=1.0, backoff_factor=2.0)
    with (
        MailInfra(api_key=TEST_API_KEY, base_url=BASE_URL, retry_policy=policy) as client,
        respx.mock(base_url=BASE_URL) as mock,
    ):
        mock.post("/emails").mock(
            side_effect=[
                httpx.Response(500),
                httpx.Response(500),
                httpx.Response(202, json=SEND_RESPONSE),
            ]
        )
        _send(client)

    # Half jitter: половина фиксированная, половина случайная, поэтому
    # проверяем коридор, а не точное значение.
    assert len(no_sleep) == 2
    assert 0.5 <= no_sleep[0] <= 1.0
    assert 1.0 <= no_sleep[1] <= 2.0


def test_retries_disabled_when_max_retries_is_zero(no_sleep: list[float]) -> None:
    with (
        MailInfra(
            api_key=TEST_API_KEY, base_url=BASE_URL, retry_policy=RetryPolicy(max_retries=0)
        ) as client,
        respx.mock(base_url=BASE_URL) as mock,
    ):
        route = mock.post("/emails").mock(return_value=httpx.Response(503))
        with pytest.raises(ServerError):
            _send(client)

    assert route.call_count == 1
    assert no_sleep == []


def test_batch_without_key_is_not_retried_on_timeout(no_sleep: list[float]) -> None:
    """Таймаут батча без ключа — не повод повторять: письма коммитятся по одному.

    Ответа нет, но это не значит, что сервер ничего не принял: часть батча
    могла уже уехать. Без ключа повтор отправил бы её второй раз.
    """
    with _client_without_auto_key() as client, respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/emails/batch").mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(APITimeoutError):
            client.emails.send_batch([BATCH_ITEM])

    assert route.call_count == 1


def test_batch_without_key_is_not_retried_on_connection_error(no_sleep: list[float]) -> None:
    """Обрыв соединения тоже неоднозначен: запрос мог дойти и выполниться."""
    with _client_without_auto_key() as client, respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/emails/batch").mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(APIConnectionError):
            client.emails.send_batch([BATCH_ITEM])

    assert route.call_count == 1


def test_cancel_is_retried_on_server_error(client: MailInfra) -> None:
    """Отмена идемпотентна по результату: повтор после `503` безопасен."""
    schedule = {"data": {"id": EMAIL_ID, "status": "CANCELED", "scheduled_at": None}}
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post(f"/emails/{EMAIL_ID}/cancel").mock(
            side_effect=[httpx.Response(503), httpx.Response(200, json=schedule)]
        )
        result = client.emails.cancel(EMAIL_ID)

    assert route.call_count == 2
    assert result.status == "CANCELED"


def test_send_without_idempotency_key_is_not_retried_on_server_error(
    no_sleep: list[float],
) -> None:
    """Без ключа бэкенду нечем распознать повтор — второй запрос создаст второе письмо.

    Ретраить `5xx` безопасно ровно потому, что SDK ставит `Idempotency-Key`.
    С `auto_idempotency=False` этой гарантии нет, значит нет и повтора.
    """
    with (
        MailInfra(
            api_key=TEST_API_KEY,
            base_url=BASE_URL,
            auto_idempotency=False,
            retry_policy=RetryPolicy(max_retries=2, initial_backoff=0.0),
        ) as client,
        respx.mock(base_url=BASE_URL) as mock,
    ):
        route = mock.post("/emails").mock(return_value=httpx.Response(503))
        with pytest.raises(ServerError):
            _send(client)

    assert route.call_count == 1


def test_send_without_idempotency_key_is_not_retried_on_timeout() -> None:
    with (
        MailInfra(
            api_key=TEST_API_KEY,
            base_url=BASE_URL,
            auto_idempotency=False,
            retry_policy=RetryPolicy(max_retries=2, initial_backoff=0.0),
        ) as client,
        respx.mock(base_url=BASE_URL) as mock,
    ):
        route = mock.post("/emails").mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(APITimeoutError):
            _send(client)

    assert route.call_count == 1


def test_send_without_idempotency_key_still_retries_rate_limit(no_sleep: list[float]) -> None:
    """`429` приходит до приёма письма — повторить можно и без ключа."""
    with (
        MailInfra(
            api_key=TEST_API_KEY,
            base_url=BASE_URL,
            auto_idempotency=False,
            retry_policy=RetryPolicy(max_retries=2, initial_backoff=0.0),
        ) as client,
        respx.mock(base_url=BASE_URL) as mock,
    ):
        route = mock.post("/emails").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}, json={}),
                httpx.Response(202, json=SEND_RESPONSE),
            ]
        )
        _send(client)

    assert route.call_count == 2


def test_custom_http_client_keeps_its_own_timeout() -> None:
    """Чужой клиент настроен вызывающим — SDK не должен затирать его Timeout.

    Раньше per-request `timeout=30.0` (float) заменял объект целиком, и
    раздельные connect/read молча терялись.
    """
    configured = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=1.0)
    with (
        httpx.Client(timeout=configured) as http_client,
        MailInfra(api_key=TEST_API_KEY, base_url=BASE_URL, http_client=http_client) as client,
    ):
        assert client.timeout is httpx.USE_CLIENT_DEFAULT

        with respx.mock(base_url=BASE_URL) as mock:
            route = mock.post("/emails").mock(return_value=httpx.Response(202, json=SEND_RESPONSE))
            _send(client)

    assert route.calls[0].request.extensions["timeout"] == {
        "connect": 5.0,
        "read": 120.0,
        "write": 10.0,
        "pool": 1.0,
    }


def test_explicit_timeout_wins_over_custom_client() -> None:
    """Явный аргумент сильнее настройки чужого клиента."""
    with (
        httpx.Client(timeout=httpx.Timeout(30.0, read=120.0)) as http_client,
        MailInfra(
            api_key=TEST_API_KEY, base_url=BASE_URL, http_client=http_client, timeout=5.0
        ) as client,
    ):
        assert client.timeout == 5.0

        with respx.mock(base_url=BASE_URL) as mock:
            route = mock.post("/emails").mock(return_value=httpx.Response(202, json=SEND_RESPONSE))
            _send(client)

    assert route.calls[0].request.extensions["timeout"]["read"] == 5.0


def test_default_timeout_is_unchanged_without_custom_client() -> None:
    """Защита от регрессии дефолта: без своего клиента по-прежнему 30 с."""
    with MailInfra(api_key=TEST_API_KEY, base_url=BASE_URL) as client:
        assert client.timeout == 30.0

        with respx.mock(base_url=BASE_URL) as mock:
            route = mock.post("/emails").mock(return_value=httpx.Response(202, json=SEND_RESPONSE))
            _send(client)

    assert route.calls[0].request.extensions["timeout"]["read"] == 30.0


def test_httpx_timeout_object_is_accepted_directly() -> None:
    """Раздельные connect/read теперь задаются без своего клиента."""
    with (
        MailInfra(
            api_key=TEST_API_KEY,
            base_url=BASE_URL,
            timeout=httpx.Timeout(connect=2.0, read=90.0, write=10.0, pool=1.0),
        ) as client,
        respx.mock(base_url=BASE_URL) as mock,
    ):
        route = mock.post("/emails").mock(return_value=httpx.Response(202, json=SEND_RESPONSE))
        _send(client)

    assert route.calls[0].request.extensions["timeout"]["read"] == 90.0
