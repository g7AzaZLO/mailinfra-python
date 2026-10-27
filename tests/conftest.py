"""Общие фикстуры тестов SDK.

Сеть не трогаем: HTTP мокается respx. Паузы между повторами тоже не
настоящие — `no_sleep` подменяет `time.sleep`/`asyncio.sleep` и записывает,
сколько SDK собирался спать. Это и ускоряет тесты, и делает backoff
наблюдаемым: длительность пауз проверяется как обычное значение.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mailinfra import MailInfra, RetryPolicy
from mailinfra.client import AsyncMailInfra

SDK_ROOT = Path(__file__).resolve().parents[1]


def _shared_file(name: str, monorepo_dir: str) -> Path:
    """Общий для всех SDK файл: OpenAPI-спека или вектора подписи вебхуков.

    В публичном репозитории SDK он лежит в `resources/`, в монорепозитории
    MailInfra — в соседнем каталоге `sdk/openapi/` или `sdk/testdata/`.

    Args:
        name: Имя файла.
        monorepo_dir: Каталог файла внутри `sdk/` монорепозитория.

    Returns:
        Путь к файлу в том раскладе, в котором запущены тесты.
    """
    mirror = SDK_ROOT / "resources" / name
    return mirror if mirror.is_file() else SDK_ROOT.parent / monorepo_dir / name


SPEC_PATH = _shared_file("mailinfra-public.json", "openapi")
VECTORS_PATH = _shared_file("webhook-signatures.json", "testdata")

BASE_URL = "https://api.mailinfra.test/v1"
TEST_API_KEY = "mi_test_0123456789abcdef0123456789abcdef"
LIVE_API_KEY = "mi_live_0123456789abcdef0123456789abcdef"

EMAIL_ID = "018e1b7a-1111-7000-aaaa-111111111111"
PROJECT_ID = "018e1b7a-2222-7000-bbbb-222222222222"

SEND_RESPONSE: dict[str, Any] = {
    "data": {"id": EMAIL_ID, "status": "QUEUED", "skipped_recipients": []}
}


@pytest.fixture(scope="session")
def spec() -> dict[str, Any]:
    """Публичная OpenAPI-спека — источник правды для моделей.

    Returns:
        Разобранный документ спеки.
    """
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def webhook_vectors() -> dict[str, Any]:
    """Вектора подписи вебхуков, сгенерированные бэкендом.

    Returns:
        Разобранный документ с кейсами.
    """
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Подменяет сон клиента и собирает запрошенные задержки.

    Returns:
        Список длительностей, которые SDK попросил проспать.
    """
    delays: list[float] = []

    def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    async def fake_async_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("mailinfra.client.time.sleep", fake_sleep)
    monkeypatch.setattr("mailinfra.client.asyncio.sleep", fake_async_sleep)
    return delays


@pytest.fixture
def fast_policy() -> RetryPolicy:
    """Политика повторов без реальных пауз.

    Returns:
        Политика с двумя повторами и нулевым базовым backoff.
    """
    return RetryPolicy(max_retries=2, initial_backoff=0.0)


@pytest.fixture
def client(fast_policy: RetryPolicy, no_sleep: list[float]) -> Iterator[MailInfra]:
    """Синхронный клиент, настроенный на тестовый базовый URL.

    Yields:
        Клиент с тестовым ключом.
    """
    with MailInfra(api_key=TEST_API_KEY, base_url=BASE_URL, retry_policy=fast_policy) as instance:
        yield instance


@pytest.fixture
async def async_client(fast_policy: RetryPolicy, no_sleep: list[float]) -> Any:
    """Асинхронный клиент, настроенный на тестовый базовый URL.

    Yields:
        Асинхронный клиент с тестовым ключом.
    """
    async with AsyncMailInfra(
        api_key=TEST_API_KEY, base_url=BASE_URL, retry_policy=fast_policy
    ) as instance:
        yield instance
