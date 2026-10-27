"""Юнит-тесты арифметики повторов: `Retry-After`, backoff, выбор статусов."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mailinfra import RetryPolicy
from mailinfra._transport import next_delay, parse_retry_after, should_retry_status
from mailinfra._version import __version__


def test_parses_retry_after_seconds() -> None:
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after(" 5 ") == 5.0
    assert parse_retry_after("0") == 0.0


def test_parses_retry_after_http_date() -> None:
    """Прокси перед API умеет присылать дату вместо секунд."""
    now = datetime(2026, 5, 11, 20, 0, 0, tzinfo=timezone.utc)
    header = (now + timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")

    assert parse_retry_after(header, now=now) == pytest.approx(30.0, abs=1)


def test_retry_after_in_the_past_is_zero() -> None:
    now = datetime(2026, 5, 11, 20, 0, 0, tzinfo=timezone.utc)
    header = (now - timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")

    assert parse_retry_after(header, now=now) == 0.0


@pytest.mark.parametrize("value", [None, "", "  ", "soon", "not-a-date"])
def test_unparsable_retry_after_is_none(value: str | None) -> None:
    assert parse_retry_after(value) is None


@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-inf", "infinity"])
def test_non_finite_retry_after_is_none(value: str) -> None:
    """`float()` их принимает, и `max(0.0, nan)` давал 0.0 — повтор без паузы.

    Заголовок приходит от прокси и WAF, которые мы не контролируем: непонятный
    `Retry-After` должен означать «нет подсказки», а не «повторяй немедленно».
    Node на тех же значениях отдаёт undefined (`Number.isFinite`).
    """
    assert parse_retry_after(value) is None


def test_non_finite_retry_after_does_not_collapse_backoff() -> None:
    policy = RetryPolicy(initial_backoff=1.0)

    assert next_delay(attempt=0, policy=policy, retry_after=parse_retry_after("nan")) > 0.0


@pytest.mark.parametrize("status_code", [408, 425, 429, 500, 502, 503, 504])
def test_idempotent_requests_retry_transient_statuses(status_code: int) -> None:
    assert should_retry_status(status_code, idempotent=True, policy=RetryPolicy())


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 409, 422])
def test_client_errors_are_not_retried(status_code: int) -> None:
    assert not should_retry_status(status_code, idempotent=True, policy=RetryPolicy())


@pytest.mark.parametrize("status_code", [500, 502, 503, 504, 408])
def test_non_idempotent_requests_do_not_retry_server_errors(status_code: int) -> None:
    """Для батча повтор после `5xx` мог бы отправить часть писем дважды."""
    assert not should_retry_status(status_code, idempotent=False, policy=RetryPolicy())


def test_non_idempotent_requests_retry_rate_limit() -> None:
    """`429` означает, что письма не приняты — повтор безопасен."""
    assert should_retry_status(429, idempotent=False, policy=RetryPolicy())


def test_backoff_without_jitter_is_exponential_and_capped() -> None:
    policy = RetryPolicy(initial_backoff=1.0, backoff_factor=2.0, max_backoff=4.0)

    assert next_delay(attempt=0, policy=policy, jitter=False) == 1.0
    assert next_delay(attempt=1, policy=policy, jitter=False) == 2.0
    assert next_delay(attempt=2, policy=policy, jitter=False) == 4.0
    assert next_delay(attempt=9, policy=policy, jitter=False) == 4.0


def test_jitter_stays_within_half_of_base() -> None:
    policy = RetryPolicy(initial_backoff=2.0)
    delays = [next_delay(attempt=0, policy=policy) for _ in range(50)]

    assert all(delay is not None and 1.0 <= delay <= 2.0 for delay in delays)
    # Размытие должно реально размывать, иначе клиенты соберутся в волны.
    assert len(set(delays)) > 1


def test_retry_after_overrides_backoff() -> None:
    policy = RetryPolicy(initial_backoff=10.0)

    assert next_delay(attempt=0, policy=policy, retry_after=2.0) == 2.0


def test_too_long_retry_after_means_do_not_wait() -> None:
    policy = RetryPolicy(max_retry_after=30.0)

    assert next_delay(attempt=0, policy=policy, retry_after=31.0) is None


def test_policy_rejects_nonsense_values() -> None:
    with pytest.raises(ValueError, match="max_retries"):
        RetryPolicy(max_retries=-1)
    with pytest.raises(ValueError, match="initial_backoff"):
        RetryPolicy(initial_backoff=-1.0)
    with pytest.raises(ValueError, match="backoff_factor"):
        RetryPolicy(backoff_factor=0.5)


def test_version_matches_pyproject() -> None:
    """`__version__` попадает в `User-Agent`, поэтому не должен отставать.

    Читаем pyproject регуляркой, а не tomllib: SDK поддерживает Python 3.10,
    где tomllib ещё нет.
    """
    import re
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    match = re.search(r'^version = "([^"]+)"', pyproject.read_text(encoding="utf-8"), re.M)

    assert match is not None
    assert match.group(1) == __version__
