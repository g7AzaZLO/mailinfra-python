"""Проверка подписи вебхуков — на векторах, сгенерированных бэкендом.

Вектора лежат в `webhook-signatures.json` (в репозитории SDK — `resources/`) и собраны скриптом
`backend/scripts/gen_webhook_vectors.py` из самой функции подписи бэкенда.
Поэтому тест ловит не «SDK согласен с собой», а «SDK согласен с отправителем».

Возраст подписи в векторах фиксированный (2024 год), поэтому проверка
времени в них отключена (`max_age_seconds=0`), а replay-защита проверяется
отдельно на свежих метках.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from mailinfra import webhooks


def _cases(vectors: dict[str, Any], *, valid: bool) -> list[dict[str, Any]]:
    """Отбирает кейсы нужного знака.

    Args:
        vectors: Документ с векторами.
        valid: Нужны ли валидные кейсы.

    Returns:
        Список кейсов.
    """
    return [case for case in vectors["cases"] if case["valid"] is valid]


def test_vectors_file_has_both_signs(webhook_vectors: dict[str, Any]) -> None:
    assert _cases(webhook_vectors, valid=True)
    assert _cases(webhook_vectors, valid=False)
    assert webhook_vectors["algorithm"]["header"] == webhooks.SIGNATURE_HEADER


def test_accepts_every_valid_vector(webhook_vectors: dict[str, Any]) -> None:
    for case in _cases(webhook_vectors, valid=True):
        assert webhooks.verify(
            body=case["body"].encode("utf-8"),
            secret=case["secret"],
            signature_header=case["signature_header"],
            max_age_seconds=0,
        ), case["name"]


def test_rejects_every_invalid_vector(webhook_vectors: dict[str, Any]) -> None:
    for case in _cases(webhook_vectors, valid=False):
        assert not webhooks.verify(
            body=case["body"].encode("utf-8"),
            secret=case["secret"],
            signature_header=case["signature_header"],
            max_age_seconds=0,
        ), case["name"]


def test_sign_matches_backend_signature(webhook_vectors: dict[str, Any]) -> None:
    """Наша `sign` обязана давать тот же hex, что бэкендовый `sign_payload`."""
    for case in _cases(webhook_vectors, valid=True):
        assert (
            webhooks.sign(
                body=case["body"].encode("utf-8"),
                secret=case["secret"],
                timestamp=case["timestamp"],
            )
            == case["signature"]
        ), case["name"]


def test_construct_event_returns_typed_event(webhook_vectors: dict[str, Any]) -> None:
    case = next(
        item for item in webhook_vectors["cases"] if item["name"] == "delivered_cyrillic_subject"
    )
    event = webhooks.construct_event(
        body=case["body"].encode("utf-8"),
        secret=case["secret"],
        signature_header=case["signature_header"],
        max_age_seconds=0,
    )

    assert event.type == "email.delivered"
    assert event.is_test is False
    assert event.email is not None
    assert event.email["subject"] == "Ваш заказ подтверждён"
    assert event.email_id == "018e1b7a-1111-7000-aaaa-111111111111"


def test_construct_event_detects_test_ping(webhook_vectors: dict[str, Any]) -> None:
    case = next(item for item in webhook_vectors["cases"] if item["name"] == "test_ping")
    event = webhooks.construct_event(
        body=case["body"].encode("utf-8"),
        secret=case["secret"],
        signature_header=case["signature_header"],
        max_age_seconds=0,
    )

    assert event.is_test is True
    assert event.email is None
    assert event.email_id is None


def test_replay_protection_rejects_old_signature() -> None:
    body = b'{"id":"evt_x","type":"test.ping","created_at":"2026-05-11T20:00:00Z","data":{}}'
    secret = "whsec_test"
    old_timestamp = int(time.time()) - 3600
    header = (
        f"t={old_timestamp},v1={webhooks.sign(body=body, secret=secret, timestamp=old_timestamp)}"
    )

    assert webhooks.verify(body=body, secret=secret, signature_header=header) is False

    with pytest.raises(webhooks.WebhookSignatureError) as excinfo:
        webhooks.construct_event(body=body, secret=secret, signature_header=header)
    assert excinfo.value.reason == "too_old"


def test_fresh_signature_passes_default_window() -> None:
    body = b'{"id":"evt_x","type":"test.ping","created_at":"2026-05-11T20:00:00Z","data":{}}'
    secret = "whsec_test"
    now = int(time.time())
    header = f"t={now},v1={webhooks.sign(body=body, secret=secret, timestamp=now)}"

    assert webhooks.verify(body=body, secret=secret, signature_header=header) is True


def test_malformed_header_reports_reason() -> None:
    with pytest.raises(webhooks.WebhookSignatureError) as excinfo:
        webhooks.construct_event(body=b"{}", secret="whsec_test", signature_header="garbage")

    assert excinfo.value.reason == "malformed_header"


def test_non_json_body_reports_reason() -> None:
    body = b"not json at all"
    secret = "whsec_test"
    now = int(time.time())
    header = f"t={now},v1={webhooks.sign(body=body, secret=secret, timestamp=now)}"

    with pytest.raises(webhooks.WebhookSignatureError) as excinfo:
        webhooks.construct_event(body=body, secret=secret, signature_header=header)

    assert excinfo.value.reason == "invalid_json"


def test_empty_secret_reports_config_error() -> None:
    """Типовой источник — `os.environ.get("MAILINFRA_WEBHOOK_SECRET", "")`.

    Молча отклонять каждую доставку нельзя: получатель не узнает, что дело в
    конфиге. Node SDK на пустом ключе падал DOMException'ом из WebCrypto.
    """
    body = b'{"id":"evt_x","type":"test.ping","created_at":"2026-05-11T20:00:00Z","data":{}}'
    now = int(time.time())
    header = f"t={now},v1=deadbeef"

    assert webhooks.verify(body=body, secret="", signature_header=header) is False

    with pytest.raises(webhooks.WebhookSignatureError) as excinfo:
        webhooks.construct_event(body=body, secret="", signature_header=header)
    assert excinfo.value.reason == "invalid_secret"
    assert "MAILINFRA_WEBHOOK_SECRET" in str(excinfo.value)


@pytest.mark.parametrize("header", ["мусор", "t=1,v1=deadbeef"])
def test_empty_secret_outranks_other_failures(header: str) -> None:
    """Пустой secret важнее битого заголовка и устаревшей подписи.

    `reason` обязан указывать на конфиг получателя, а не на отправителя:
    Node SDK проверял secret последним и отвечал здесь `malformed_header`.
    """
    body = b'{"id":"evt_x","type":"test.ping","created_at":"2026-05-11T20:00:00Z","data":{}}'

    with pytest.raises(webhooks.WebhookSignatureError) as excinfo:
        webhooks.construct_event(body=body, secret="", signature_header=header)
    assert excinfo.value.reason == "invalid_secret"


def test_header_with_space_after_comma_is_accepted() -> None:
    """Пробел после запятой легален в HTTP, и прокси его добавляют.

    Без `strip()` разбор падал на KeyError, хотя Node SDK такой заголовок
    принимал — один и тот же вебхук доходил не везде.
    """
    body = b'{"id":"evt_x","type":"test.ping","created_at":"2026-05-11T20:00:00Z","data":{}}'
    secret = "whsec_test"
    now = int(time.time())
    header = f"t={now}, v1={webhooks.sign(body=body, secret=secret, timestamp=now)}"

    assert webhooks.verify(body=body, secret=secret, signature_header=header) is True


def test_signed_but_incomplete_event_reports_reason() -> None:
    """Событие не по контракту — документированная ошибка, а не ValidationError.

    Обработчик, написанный по docstring (`except WebhookSignatureError`),
    отдавал бы 500, и доставка ретраилась бы бесконечно.
    """
    body = b'{"id":"evt_1","data":{}}'
    secret = "whsec_test"
    now = int(time.time())
    header = f"t={now},v1={webhooks.sign(body=body, secret=secret, timestamp=now)}"

    with pytest.raises(webhooks.WebhookSignatureError) as excinfo:
        webhooks.construct_event(body=body, secret=secret, signature_header=header)

    assert excinfo.value.reason == "invalid_payload"


def test_non_ascii_signature_is_rejected_not_crashed() -> None:
    """Мусор в `v1` — это «подпись не сошлась», а не 500 у получателя.

    `hmac.compare_digest` на строке с не-ASCII символом бросает `TypeError`,
    а заголовок целиком контролирует отправитель запроса.
    """
    body = b'{"id":"evt_x","type":"test.ping","created_at":"2026-05-11T20:00:00Z","data":{}}'
    now = int(time.time())
    header = f"t={now},v1=подпись"

    assert webhooks.verify(body=body, secret="whsec_test", signature_header=header) is False

    with pytest.raises(webhooks.WebhookSignatureError) as excinfo:
        webhooks.construct_event(body=body, secret="whsec_test", signature_header=header)
    assert excinfo.value.reason == "signature_mismatch"


ROTATION_BODY = (
    b'{"id":"evt_x","type":"email.scheduled","created_at":"2026-05-11T20:00:00Z",'
    b'"data":{"email":{"id":"018e1b7a-1111-7000-aaaa-111111111111",'
    b'"metadata":{"order_id":"4242"},"status":"SCHEDULED"},"event":{}}}'
)
NEW_SECRET = "whsec_new"
OLD_SECRET = "whsec_old"


def _rotation_header(body: bytes, *secrets: str) -> str:
    """Заголовок с подписью каждым из секретов — как в окне ротации.

    Args:
        body: Сырое тело.
        *secrets: Секреты в порядке подписей.

    Returns:
        Значение `X-MailInfra-Signature` со свежей меткой времени.
    """
    now = int(time.time())
    signatures = ",".join(
        f"v1={webhooks.sign(body=body, secret=secret, timestamp=now)}" for secret in secrets
    )
    return f"t={now},{signatures}"


def test_vectors_cover_secret_rotation(webhook_vectors: dict[str, Any]) -> None:
    """Общие вектора обязаны проверять окно ротации — иначе тест выше пустой."""
    cases = {case["name"]: case for case in webhook_vectors["cases"]}

    assert cases["rotation_new_secret"]["valid"] is True
    assert cases["rotation_previous_secret"]["valid"] is True
    assert cases["rotation_unknown_secret"]["valid"] is False
    assert cases["rotation_previous_secret"]["signature_header"].count("v1=") == 2


@pytest.mark.parametrize("secret", [NEW_SECRET, OLD_SECRET])
@pytest.mark.parametrize("order", ["new_first", "old_first"])
def test_rotation_accepts_any_matching_signature(secret: str, order: str) -> None:
    """Совпала любая `v1` — подпись верна, где бы она ни стояла в заголовке.

    Разбор в словарь оставлял последнюю `v1`: обработчик с новым секретом
    отвергал события сразу после ротации.
    """
    secrets = (NEW_SECRET, OLD_SECRET) if order == "new_first" else (OLD_SECRET, NEW_SECRET)
    header = _rotation_header(ROTATION_BODY, *secrets)

    assert webhooks.verify(body=ROTATION_BODY, secret=secret, signature_header=header) is True
    event = webhooks.construct_event(body=ROTATION_BODY, secret=secret, signature_header=header)
    assert event.type == "email.scheduled"


def test_rotation_rejects_unknown_secret() -> None:
    header = _rotation_header(ROTATION_BODY, NEW_SECRET, OLD_SECRET)

    assert (
        webhooks.verify(body=ROTATION_BODY, secret="whsec_other", signature_header=header) is False
    )
    with pytest.raises(webhooks.WebhookSignatureError) as excinfo:
        webhooks.construct_event(body=ROTATION_BODY, secret="whsec_other", signature_header=header)
    assert excinfo.value.reason == "signature_mismatch"


def test_rotation_header_with_garbage_signature_still_verifies() -> None:
    """Мусор в одной `v1` не роняет проверку остальных."""
    now = int(time.time())
    valid = webhooks.sign(body=ROTATION_BODY, secret=NEW_SECRET, timestamp=now)
    header = f"t={now},v1=подпись,v1={valid}"

    assert webhooks.verify(body=ROTATION_BODY, secret=NEW_SECRET, signature_header=header) is True


def test_unknown_signature_scheme_is_ignored() -> None:
    """Будущая схема (`v2=…`) рядом с `v1` не ломает разбор."""
    now = int(time.time())
    valid = webhooks.sign(body=ROTATION_BODY, secret=NEW_SECRET, timestamp=now)
    header = f"t={now},v2=deadbeef,v1={valid}"

    assert webhooks.verify(body=ROTATION_BODY, secret=NEW_SECRET, signature_header=header) is True


@pytest.mark.parametrize("header", ["t=1715461200", "v1=deadbeef", "t=soon,v1=deadbeef"])
def test_header_without_timestamp_or_signature_is_malformed(header: str) -> None:
    with pytest.raises(webhooks.WebhookSignatureError) as excinfo:
        webhooks.construct_event(body=b"{}", secret="whsec_test", signature_header=header)

    assert excinfo.value.reason == "malformed_header"


def test_event_exposes_email_metadata() -> None:
    header = _rotation_header(ROTATION_BODY, NEW_SECRET)
    event = webhooks.construct_event(body=ROTATION_BODY, secret=NEW_SECRET, signature_header=header)

    assert event.metadata == {"order_id": "4242"}


def test_event_without_email_has_empty_metadata(webhook_vectors: dict[str, Any]) -> None:
    case = next(item for item in webhook_vectors["cases"] if item["name"] == "test_ping")
    event = webhooks.construct_event(
        body=case["body"].encode("utf-8"),
        secret=case["secret"],
        signature_header=case["signature_header"],
        max_age_seconds=0,
    )

    assert event.metadata == {}
