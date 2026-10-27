"""Клиенты MailInfra: синхронный `MailInfra` и асинхронный `AsyncMailInfra`.

Оба поверх httpx и оба реализуют один и тот же контракт:

* аутентификация по API-ключу проекта (`mi_live_…` / `mi_test_…`);
* автоматический `Idempotency-Key` на отправке письма;
* повторы с экспоненциальным backoff и уважением `Retry-After`;
* ошибки API, поднятые как типизированные исключения (`mailinfra.errors`).

Решение «повторять или нет» живёт в `_transport`, разбор ошибок — в `_errors`,
поэтому здесь остаётся только цикл попыток: один синхронный, один
асинхронный. Отличаются они ровно двумя строками (`await` и способ поспать),
всё остальное — общие методы `_BaseClient`.
"""

from __future__ import annotations

import asyncio
import os
import platform
import time
from types import TracebackType
from typing import Any, Literal

import httpx

from ._errors import (
    APIConnectionError,
    APITimeoutError,
    MailInfraError,
    error_from_response,
)
from ._transport import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    RetryPolicy,
    next_delay,
    parse_retry_after,
    should_retry_status,
)
from ._version import __version__
from .resources.emails import AsyncEmailsResource, EmailsResource

__all__ = ["AsyncMailInfra", "MailInfra"]

API_KEY_ENV_VAR = "MAILINFRA_API_KEY"
BASE_URL_ENV_VAR = "MAILINFRA_BASE_URL"

REQUEST_ID_HEADER = "X-Request-ID"
IDEMPOTENT_REPLAYED_HEADER = "Idempotent-Replayed"

Environment = Literal["live", "test"]

# Регистр префикса важен только для распознавания окружения: ключи, выданные
# до фикса генератора, имеют вид `mi_LIVE_…` (см. legacy-ветку в
# `app.api.api_auth.get_api_context`), и такой ключ обязан работать.
_LIVE_PREFIXES = ("mi_live_", "mi_LIVE_")
_TEST_PREFIXES = ("mi_test_", "mi_TEST_")


def _detect_environment(api_key: str) -> Environment:
    """Определяет окружение ключа по его префиксу.

    Args:
        api_key: API-ключ проекта.

    Returns:
        `"live"` для рабочих ключей, `"test"` для sandbox.

    Raises:
        ValueError: Если префикс не распознан — лучше упасть при создании
            клиента, чем получить `401` на первой отправке.
    """
    if api_key.startswith(_LIVE_PREFIXES):
        return "live"
    if api_key.startswith(_TEST_PREFIXES):
        return "test"
    raise ValueError(
        "API-ключ MailInfra должен начинаться с 'mi_live_' или 'mi_test_'. "
        "Ключ создаётся в дашборде и показывается один раз."
    )


def _user_agent(suffix: str | None) -> str:
    """Собирает `User-Agent` клиента.

    Args:
        suffix: Дополнение вызывающего приложения (например, имя интеграции).

    Returns:
        Строка вида `mailinfra-python/0.1.0 (httpx/0.28.1; CPython/3.12.3)`.
    """
    base = (
        f"mailinfra-python/{__version__} "
        f"(httpx/{httpx.__version__}; {platform.python_implementation()}/"
        f"{platform.python_version()})"
    )
    return f"{base} {suffix}" if suffix else base


class _BaseClient:
    """Общая часть синхронного и асинхронного клиентов.

    Держит конфигурацию, сборку заголовков, разбор ответа и решения о
    повторах. Сетевого ввода-вывода здесь нет — он в наследниках.
    """

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str | None,
        timeout: float | httpx.Timeout | None,
        owns_client: bool,
        max_retries: int,
        retry_policy: RetryPolicy | None,
        auto_idempotency: bool,
        require_environment: Environment | None,
        user_agent_suffix: str | None,
    ) -> None:
        resolved_key = api_key or os.environ.get(API_KEY_ENV_VAR)
        if not resolved_key:
            raise ValueError(
                "Не задан API-ключ: передайте api_key=… или положите ключ в "
                f"переменную окружения {API_KEY_ENV_VAR}."
            )

        self._api_key = resolved_key
        self.environment: Environment = _detect_environment(resolved_key)

        if require_environment and self.environment != require_environment:
            raise ValueError(
                f"Ожидался ключ окружения '{require_environment}', "
                f"а передан '{self.environment}'. Проверьте конфигурацию: "
                "тестовый ключ в проде не отправит ни одного письма, а "
                "рабочий в тестах отправит настоящие."
            )

        self.base_url = (base_url or os.environ.get(BASE_URL_ENV_VAR) or DEFAULT_BASE_URL).rstrip(
            "/"
        )
        # У таймаута три состояния, а не два: `None` — «вызывающий не
        # высказался». Для своего клиента это дефолт SDK, а для чужого —
        # `USE_CLIENT_DEFAULT`: иначе float затёр бы настроенный вызывающим
        # `Timeout(connect=…, read=…)` целиком. Явный аргумент сильнее и того,
        # и другого.
        #
        # Тип — `Any`, потому что `UseClientDefault` живёт в `httpx._client`
        # и публичного алиаса у него нет (как и у `TimeoutTypes`).
        self.timeout: Any
        if timeout is not None:
            self.timeout = timeout
        elif owns_client:
            self.timeout = DEFAULT_TIMEOUT_SECONDS
        else:
            self.timeout = httpx.USE_CLIENT_DEFAULT
        self.auto_idempotency = auto_idempotency
        self.retry_policy = retry_policy or RetryPolicy(max_retries=max_retries)
        self._user_agent = _user_agent(user_agent_suffix)

    # ---------- подготовка запроса ----------

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Собирает заголовки запроса.

        Args:
            extra: Дополнительные заголовки (например, `Idempotency-Key`).

        Returns:
            Словарь заголовков с авторизацией и `User-Agent`.
        """
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        }
        if extra:
            headers.update({key: value for key, value in extra.items() if value is not None})
        return headers

    # ---------- разбор ответа ----------

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        """Разбирает тело ответа.

        Args:
            response: Ответ httpx.

        Returns:
            Разобранный JSON, либо текст, если это не JSON (прокси и
            балансировщики умеют отвечать HTML).
        """
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def _unwrap(self, response: httpx.Response) -> Any:
        """Достаёт поле `data` из успешного ответа.

        Args:
            response: Успешный ответ API.

        Returns:
            Значение `data`.

        Raises:
            MailInfraError: Если ответ успешен, но не по контракту — иначе
                ошибка всплыла бы как невнятный `KeyError` при разборе модели.
        """
        body = self._decode(response)
        if not isinstance(body, dict) or "data" not in body:
            raise MailInfraError(
                "Ответ API не по контракту: ожидался объект с полем 'data'",
                status_code=response.status_code,
                code="malformed_response",
                request_id=response.headers.get(REQUEST_ID_HEADER),
            )
        return body["data"]

    def _error(self, response: httpx.Response) -> MailInfraError:
        """Строит исключение по неуспешному ответу.

        Args:
            response: Ответ API со статусом >= 400.

        Returns:
            Типизированное исключение.
        """
        retry_after = parse_retry_after(response.headers.get("Retry-After"))
        return error_from_response(
            status_code=response.status_code,
            body=self._decode(response),
            request_id=response.headers.get(REQUEST_ID_HEADER),
            # `round`, а не `int`: Node отдаёт `Math.round`, и на
            # `Retry-After: 0.9` усечение давало 0 — «повторить сейчас»,
            # то есть busy-loop там, где Node ждёт секунду.
            retry_after=None if retry_after is None else round(retry_after),
        )

    # ---------- решения о повторах ----------

    def _delay_after_response(
        self, response: httpx.Response, *, attempt: int, idempotent: bool
    ) -> float | None:
        """Считает паузу перед повтором после неуспешного ответа.

        Args:
            response: Неуспешный ответ API.
            attempt: Номер завершившейся попытки, с 0.
            idempotent: Безопасно ли повторять этот запрос.

        Returns:
            Пауза в секундах или `None`, если повторять не нужно.
        """
        if attempt >= self.retry_policy.max_retries:
            return None
        if not should_retry_status(
            response.status_code, idempotent=idempotent, policy=self.retry_policy
        ):
            return None
        return next_delay(
            attempt=attempt,
            policy=self.retry_policy,
            retry_after=parse_retry_after(response.headers.get("Retry-After")),
        )

    def _delay_after_network_error(self, *, attempt: int, idempotent: bool) -> float | None:
        """Считает паузу перед повтором после сетевой ошибки.

        Отсутствие ответа НЕ означает, что сервер ничего не принял: таймаут
        чтения и обрыв соединения случаются и после того, как запрос дошёл и
        был выполнен. Поэтому неидемпотентный запрос (батч, отправка без
        `Idempotency-Key`) здесь не повторяется — повтор отправил бы письма
        второй раз, а это хуже, чем честный отказ.

        Args:
            attempt: Номер завершившейся попытки, с 0.
            idempotent: Безопасно ли повторять этот запрос.

        Returns:
            Пауза в секундах или `None`, если повторять не нужно.
        """
        if not idempotent:
            return None
        if attempt >= self.retry_policy.max_retries:
            return None
        return next_delay(attempt=attempt, policy=self.retry_policy)


class MailInfra(_BaseClient):
    """Синхронный клиент MailInfra.

    Пример::

        from mailinfra import MailInfra

        with MailInfra(api_key="mi_live_…") as client:
            result = client.emails.send(
                from_email="hello@mail.example.ru",
                to="user@example.com",
                subject="Привет",
                html="<p>Привет!</p>",
            )
            print(result.id, result.status)

    Без `api_key` ключ берётся из переменной окружения `MAILINFRA_API_KEY`.

    Attributes:
        emails: Ресурс писем (`send`, `send_batch`, `get`, `list`, `events`).
        environment: `"live"` или `"test"` — распознано по префиксу ключа.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int = 2,
        retry_policy: RetryPolicy | None = None,
        auto_idempotency: bool = True,
        require_environment: Environment | None = None,
        http_client: httpx.Client | None = None,
        user_agent_suffix: str | None = None,
    ) -> None:
        """Создаёт клиент.

        Args:
            api_key: API-ключ проекта; по умолчанию из `MAILINFRA_API_KEY`.
            base_url: База API; по умолчанию из `MAILINFRA_BASE_URL` либо
                `https://app.mailinfra.ru/v1`.
            timeout: Таймаут одного запроса: секунды или `httpx.Timeout`
                для раздельных connect/read/write/pool. По умолчанию
                `30` с, а если передан свой `http_client` — берётся
                его настройка.
            max_retries: Сколько раз повторять сверх первой попытки.
            retry_policy: Полная политика повторов; переопределяет
                `max_retries`.
            auto_idempotency: Ставить ли `Idempotency-Key` автоматически.
            require_environment: Требуемое окружение ключа; при
                несовпадении клиент не создастся.
            http_client: Свой `httpx.Client` (прокси, пул, mTLS). Его
                таймаут SDK не перетирает, если `timeout` не задан явно.
                Закрытие остаётся на вызывающем.
            user_agent_suffix: Дополнение к `User-Agent`.
        """
        super().__init__(
            api_key,
            base_url=base_url,
            timeout=timeout,
            owns_client=http_client is None,
            max_retries=max_retries,
            retry_policy=retry_policy,
            auto_idempotency=auto_idempotency,
            require_environment=require_environment,
            user_agent_suffix=user_agent_suffix,
        )
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(timeout=self.timeout)
        self.emails = EmailsResource(self)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        idempotent: bool = True,
    ) -> Any:
        """Выполняет запрос к API с повторами и разбором ошибок.

        Args:
            method: HTTP-метод.
            path: Путь относительно базы (`/emails`).
            json: Тело запроса.
            params: Query-параметры.
            headers: Дополнительные заголовки.
            idempotent: Можно ли безопасно повторить запрос.

        Returns:
            Содержимое поля `data` успешного ответа.

        Raises:
            MailInfraError: Любая ошибка API или транспорта.
        """
        data, _ = self.request_with_response(
            method, path, json=json, params=params, headers=headers, idempotent=idempotent
        )
        return data

    def request_with_response(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        idempotent: bool = True,
    ) -> tuple[Any, httpx.Response]:
        """Выполняет запрос и отдаёт вместе с данными сам ответ.

        Ответ нужен там, где важны заголовки: `Idempotent-Replayed` при
        отправке письма и `X-Request-ID` для логов.

        Args:
            method: HTTP-метод.
            path: Путь относительно базы.
            json: Тело запроса.
            params: Query-параметры.
            headers: Дополнительные заголовки.
            idempotent: Можно ли безопасно повторить запрос.

        Returns:
            Пара «данные, ответ».

        Raises:
            MailInfraError: Любая ошибка API или транспорта.
        """
        url = f"{self.base_url}{path}"
        request_headers = self._headers(headers)
        attempt = 0

        while True:
            try:
                response = self._http.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers=request_headers,
                    timeout=self.timeout,
                )
            except httpx.TimeoutException as exc:
                delay = self._delay_after_network_error(attempt=attempt, idempotent=idempotent)
                if delay is None:
                    raise APITimeoutError() from exc
                time.sleep(delay)
                attempt += 1
                continue
            except httpx.HTTPError as exc:
                delay = self._delay_after_network_error(attempt=attempt, idempotent=idempotent)
                if delay is None:
                    raise APIConnectionError() from exc
                time.sleep(delay)
                attempt += 1
                continue

            if response.is_success:
                return self._unwrap(response), response

            delay = self._delay_after_response(response, attempt=attempt, idempotent=idempotent)
            if delay is None:
                raise self._error(response)

            time.sleep(delay)
            attempt += 1

    def close(self) -> None:
        """Закрывает HTTP-клиент, если он создан самим SDK."""
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> MailInfra:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class AsyncMailInfra(_BaseClient):
    """Асинхронный клиент MailInfra.

    Пример::

        from mailinfra import AsyncMailInfra

        async with AsyncMailInfra(api_key="mi_live_…") as client:
            result = await client.emails.send(
                from_email="hello@mail.example.ru",
                to="user@example.com",
                subject="Привет",
                text="Привет!",
            )

    Attributes:
        emails: Ресурс писем.
        environment: `"live"` или `"test"`.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int = 2,
        retry_policy: RetryPolicy | None = None,
        auto_idempotency: bool = True,
        require_environment: Environment | None = None,
        http_client: httpx.AsyncClient | None = None,
        user_agent_suffix: str | None = None,
    ) -> None:
        """Создаёт асинхронный клиент.

        Args:
            api_key: API-ключ проекта; по умолчанию из `MAILINFRA_API_KEY`.
            base_url: База API.
            timeout: Таймаут одного запроса: секунды или `httpx.Timeout`
                для раздельных connect/read/write/pool. По умолчанию
                `30` с, а если передан свой `http_client` — берётся
                его настройка.
            max_retries: Сколько раз повторять сверх первой попытки.
            retry_policy: Полная политика повторов.
            auto_idempotency: Ставить ли `Idempotency-Key` автоматически.
            require_environment: Требуемое окружение ключа.
            http_client: Свой `httpx.AsyncClient`. Его таймаут SDK не
                перетирает, если `timeout` не задан явно.
            user_agent_suffix: Дополнение к `User-Agent`.
        """
        super().__init__(
            api_key,
            base_url=base_url,
            timeout=timeout,
            owns_client=http_client is None,
            max_retries=max_retries,
            retry_policy=retry_policy,
            auto_idempotency=auto_idempotency,
            require_environment=require_environment,
            user_agent_suffix=user_agent_suffix,
        )
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=self.timeout)
        self.emails = AsyncEmailsResource(self)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        idempotent: bool = True,
    ) -> Any:
        """Выполняет запрос к API с повторами и разбором ошибок.

        Args:
            method: HTTP-метод.
            path: Путь относительно базы.
            json: Тело запроса.
            params: Query-параметры.
            headers: Дополнительные заголовки.
            idempotent: Можно ли безопасно повторить запрос.

        Returns:
            Содержимое поля `data` успешного ответа.

        Raises:
            MailInfraError: Любая ошибка API или транспорта.
        """
        data, _ = await self.request_with_response(
            method, path, json=json, params=params, headers=headers, idempotent=idempotent
        )
        return data

    async def request_with_response(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        idempotent: bool = True,
    ) -> tuple[Any, httpx.Response]:
        """Выполняет запрос и отдаёт вместе с данными сам ответ.

        Args:
            method: HTTP-метод.
            path: Путь относительно базы.
            json: Тело запроса.
            params: Query-параметры.
            headers: Дополнительные заголовки.
            idempotent: Можно ли безопасно повторить запрос.

        Returns:
            Пара «данные, ответ».

        Raises:
            MailInfraError: Любая ошибка API или транспорта.
        """
        url = f"{self.base_url}{path}"
        request_headers = self._headers(headers)
        attempt = 0

        while True:
            try:
                response = await self._http.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers=request_headers,
                    timeout=self.timeout,
                )
            except httpx.TimeoutException as exc:
                delay = self._delay_after_network_error(attempt=attempt, idempotent=idempotent)
                if delay is None:
                    raise APITimeoutError() from exc
                await asyncio.sleep(delay)
                attempt += 1
                continue
            except httpx.HTTPError as exc:
                delay = self._delay_after_network_error(attempt=attempt, idempotent=idempotent)
                if delay is None:
                    raise APIConnectionError() from exc
                await asyncio.sleep(delay)
                attempt += 1
                continue

            if response.is_success:
                return self._unwrap(response), response

            delay = self._delay_after_response(response, attempt=attempt, idempotent=idempotent)
            if delay is None:
                raise self._error(response)

            await asyncio.sleep(delay)
            attempt += 1

    async def aclose(self) -> None:
        """Закрывает HTTP-клиент, если он создан самим SDK."""
        if self._owns_client:
            await self._http.aclose()

    async def __aenter__(self) -> AsyncMailInfra:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()
