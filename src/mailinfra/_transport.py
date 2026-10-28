"""Политика повторов, backoff и разбор `Retry-After`.

Синхронный и асинхронный клиенты отличаются только тем, как они спят и ждут
ответ, поэтому вся арифметика решения «повторять или нет» живёт здесь и
тестируется без сети.

Ключевое различие между запросами — идемпотентность:

* `POST /emails` идемпотентен: SDK сам ставит `Idempotency-Key` и
  переиспользует его на повторах, поэтому ретраить `5xx` и таймауты
  безопасно — бэкенд отдаст тот же результат, а не отправит письмо дважды.
* `POST /emails/batch` с ключом тоже идемпотентен: письма батча коммитятся
  по одному, но бэкенд выводит из ключа запроса ключ каждого письма, и
  повтор не дублирует уже принятые.
* Отправка и батч без `Idempotency-Key` (`auto_idempotency=False`)
  неидемпотентны: бэкенд дедуплицирует только по ключу. Повтор после `5xx`,
  таймаута или обрыва соединения может отправить письма второй раз, поэтому
  для них повторяется единственная ситуация, в которой заведомо ничего не
  принято: `429` от лимитёра, который отказывает до приёма писем.
"""

from __future__ import annotations

import email.utils
import math
import random
from dataclasses import dataclass, field

# `datetime.UTC` появился только в 3.11, а SDK заявляет поддержку 3.10 —
# поэтому здесь `timezone.utc`, а не более короткий алиас.
from datetime import datetime, timezone

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "RetryPolicy",
    "next_delay",
    "parse_retry_after",
    "should_retry_status",
]

DEFAULT_BASE_URL = "https://app.mailinfra.ru/v1"
DEFAULT_TIMEOUT_SECONDS = 30.0

# Статусы, которые имеет смысл повторять. 429 — лимит (бэкенд присылает
# `Retry-After`), 408/425 — таймаут/ранние данные на стороне прокси,
# 5xx — временные отказы. 409 `idempotency_in_flight` сознательно НЕ здесь:
# параллельный запрос с тем же ключом ещё выполняется, и корректная реакция —
# отдать управление вызывающему, а не крутить повторы вслепую.
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Для неидемпотентных запросов повторяем только то, где ничего не принято.
RETRYABLE_STATUSES_NON_IDEMPOTENT = frozenset({429})


@dataclass(frozen=True)
class RetryPolicy:
    """Настройки повторов.

    Attributes:
        max_retries: Сколько раз повторить сверх первой попытки. `0` —
            повторов нет.
        initial_backoff: Базовая задержка первой паузы, секунды.
        backoff_factor: Множитель роста задержки между попытками.
        max_backoff: Потолок собственной задержки, секунды.
        max_retry_after: Порог доверия к `Retry-After`. Если сервер просит
            подождать дольше — SDK не спит, а отдаёт `RateLimitError`,
            чтобы вызывающий сам решил, ждать или деградировать.
        retryable_statuses: Статусы, повторяемые для идемпотентных запросов.
        retryable_statuses_non_idempotent: То же для неидемпотентных.
    """

    max_retries: int = 2
    initial_backoff: float = 0.5
    backoff_factor: float = 2.0
    max_backoff: float = 8.0
    max_retry_after: float = 60.0
    retryable_statuses: frozenset[int] = field(default=RETRYABLE_STATUSES)
    retryable_statuses_non_idempotent: frozenset[int] = field(
        default=RETRYABLE_STATUSES_NON_IDEMPOTENT
    )

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries не может быть отрицательным")
        if self.initial_backoff < 0:
            raise ValueError("initial_backoff не может быть отрицательным")
        if self.backoff_factor < 1:
            raise ValueError("backoff_factor должен быть >= 1")


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Разбирает заголовок `Retry-After`.

    Бэкенд MailInfra присылает секунды (`app.main.register_exception_handlers`),
    но HTTP допускает и дату — её умеют присылать прокси и CDN перед API,
    поэтому поддерживаются оба формата.

    Args:
        value: Значение заголовка или `None`.
        now: Момент отсчёта для формата-даты; по умолчанию текущее время.

    Returns:
        Неотрицательная задержка в секундах или `None`, если заголовка нет
        либо он не разобран.
    """
    if value is None:
        return None

    raw = value.strip()
    if not raw:
        return None

    try:
        seconds = float(raw)
    except ValueError:
        seconds = None
    if seconds is not None:
        # `float()` принимает "nan" и "inf". На `nan` сравнение внутри `max`
        # ложно, и `max(0.0, nan)` возвращает 0.0 — заголовок от прокси или WAF
        # превращал 429 в повтор без паузы, ровно вместо того backoff'а, ради
        # которого он и прислан. Node здесь `Number.isFinite`, держим паритет.
        if not math.isfinite(seconds):
            return None
        return max(0.0, seconds)

    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        # `parsedate_to_datetime` на мусоре бросает ValueError (а не отдаёт
        # None, как можно решить по документации). Заголовок от прокси или
        # WAF мы не контролируем, и падать из-за него нельзя: непонятный
        # `Retry-After` — это просто «нет подсказки», дальше работает
        # собственный backoff.
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    reference = now or datetime.now(timezone.utc)
    return max(0.0, (parsed - reference).total_seconds())


def should_retry_status(status_code: int, *, idempotent: bool, policy: RetryPolicy) -> bool:
    """Повторять ли запрос с таким статусом.

    Args:
        status_code: HTTP-статус ответа.
        idempotent: Можно ли безопасно повторить запрос (для отправки с
            `Idempotency-Key` — да, SDK переиспользует ключ; без ключа — нет).
        policy: Действующая политика повторов.

    Returns:
        `True`, если статус в списке повторяемых.
    """
    allowed = policy.retryable_statuses if idempotent else policy.retryable_statuses_non_idempotent
    return status_code in allowed


def next_delay(
    *,
    attempt: int,
    policy: RetryPolicy,
    retry_after: float | None = None,
    jitter: bool = True,
) -> float | None:
    """Считает паузу перед следующей попыткой.

    `Retry-After` приоритетнее собственного backoff: сервер знает про свои
    окна лимитов больше, чем клиент. Но доверие ограничено `max_retry_after` —
    иначе один длинный `Retry-After` подвесил бы вызывающий код на минуты.

    Args:
        attempt: Номер завершившейся попытки, начиная с 0.
        policy: Действующая политика повторов.
        retry_after: Разобранное значение `Retry-After`, если было.
        jitter: Размывать ли собственную задержку. Отключается в тестах.

    Returns:
        Задержка в секундах или `None`, если ждать не следует (сервер просит
        слишком долго — пусть решает вызывающий).
    """
    if retry_after is not None:
        if retry_after > policy.max_retry_after:
            return None
        return retry_after

    base = min(policy.max_backoff, policy.initial_backoff * policy.backoff_factor**attempt)
    if not jitter:
        return base

    # Half jitter: половина задержки фиксированная, половина случайная. Полный
    # jitter иногда даёт почти нулевую паузу и повтор улетает в тот же
    # перегруженный сервер; фиксированная пауза без jitter собирает
    # клиентов в синхронные волны.
    return base / 2 + random.random() * base / 2  # noqa: S311 — не криптография
