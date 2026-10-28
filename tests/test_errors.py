"""Маппинг конверта ошибок в типизированные исключения."""

from __future__ import annotations

import httpx
import pytest
import respx

from mailinfra import (
    AllRecipientsSuppressedError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    DomainNotAttachedError,
    DomainNotVerifiedError,
    EmailNotScheduledError,
    IdempotencyInFlightError,
    IPNotAllowedError,
    LegalDocumentsNotAcceptedError,
    MailInfra,
    MailInfraError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ReservedEmailHeaderError,
    SandboxRecipientNotAllowedError,
    SandboxTrialExhaustedError,
    SendAtInvalidError,
    SendAtTooFarError,
    ServerError,
    TemplateRenderError,
    TemplateVariableMissingError,
    UnprocessableError,
)
from mailinfra._errors import error_from_response

from .conftest import BASE_URL, LIVE_API_KEY, TEST_API_KEY


def _envelope(code: str, message: str = "Ошибка", details: dict | None = None) -> dict:
    """Собирает конверт ошибки API.

    Args:
        code: Машинный код.
        message: Человеко-читаемое сообщение.
        details: Детали ошибки.

    Returns:
        Тело ответа в формате https://docs.mailinfra.ru/errors.
    """
    return {
        "error": {"code": code, "message": message, "details": details or {}},
        "request_id": "req-test",
    }


@pytest.mark.parametrize(
    ("status_code", "code", "expected"),
    [
        (401, "unauthorized", AuthenticationError),
        (403, "forbidden", PermissionDeniedError),
        (404, "not_found", NotFoundError),
        (409, "conflict", ConflictError),
        (429, "rate_limit_exceeded", RateLimitError),
        (500, "internal_error", ServerError),
        (503, "service_unavailable", ServerError),
        (418, "teapot", MailInfraError),
    ],
)
def test_status_maps_to_exception(status_code: int, code: str, expected: type) -> None:
    error = error_from_response(
        status_code=status_code, body=_envelope(code), request_id=None, retry_after=None
    )

    assert isinstance(error, expected)
    assert error.code == code
    assert error.request_id == "req-test"


@pytest.mark.parametrize(
    ("status_code", "code", "expected", "category"),
    [
        (422, "domain_not_verified", DomainNotVerifiedError, UnprocessableError),
        (422, "domain_not_attached", DomainNotAttachedError, UnprocessableError),
        (422, "all_recipients_suppressed", AllRecipientsSuppressedError, UnprocessableError),
        (
            422,
            "sandbox_recipient_not_allowed",
            SandboxRecipientNotAllowedError,
            UnprocessableError,
        ),
        (422, "template_render_error", TemplateRenderError, UnprocessableError),
        (422, "template_variable_missing", TemplateVariableMissingError, UnprocessableError),
        (422, "reserved_email_header", ReservedEmailHeaderError, UnprocessableError),
        (
            422,
            "legal_documents_not_accepted",
            LegalDocumentsNotAcceptedError,
            UnprocessableError,
        ),
        (403, "sandbox_trial_exhausted", SandboxTrialExhaustedError, PermissionDeniedError),
        (409, "idempotency_in_flight", IdempotencyInFlightError, ConflictError),
        (409, "email_not_scheduled", EmailNotScheduledError, ConflictError),
        (400, "send_at_too_far", SendAtTooFarError, BadRequestError),
        (400, "send_at_invalid", SendAtInvalidError, BadRequestError),
        (403, "ip_not_allowed", IPNotAllowedError, PermissionDeniedError),
        (429, "rps_exceeded", RateLimitError, RateLimitError),
    ],
)
def test_domain_code_maps_to_specific_exception(
    status_code: int, code: str, expected: type, category: type
) -> None:
    """Конкретный класс остаётся и классом своей категории.

    Статусы — те, с которыми бэкенд реально отдаёт код (места `raise` в
    `app.services`); `except UnprocessableError` из README обязан ловить и
    `DomainNotVerifiedError`. Бэкенд поднимает `details.code` в `error.code`
    (`app.main.translated_error`), так что в реальном конверте код продублирован.
    """
    error = error_from_response(
        status_code=status_code,
        body=_envelope(code, details={"code": code}),
        request_id=None,
        retry_after=None,
    )

    assert isinstance(error, expected)
    assert isinstance(error, category)
    assert error.code == code
    assert error.status_code == status_code


def test_legal_documents_error_carries_pending_list() -> None:
    pending = [{"type": "DPA", "version": "2", "id": "doc-1"}]
    error = error_from_response(
        status_code=422,
        body=_envelope(
            "legal_documents_not_accepted",
            details={"code": "legal_documents_not_accepted", "pending": pending},
        ),
        request_id=None,
        retry_after=None,
    )

    assert isinstance(error, LegalDocumentsNotAcceptedError)
    assert error.details["pending"] == pending


def test_email_not_scheduled_carries_current_status() -> None:
    """По `details.status` видно, почему отмена не прошла: письмо уже ушло или отменено."""
    error = error_from_response(
        status_code=409,
        body=_envelope(
            "email_not_scheduled",
            details={"code": "email_not_scheduled", "status": "CANCELED"},
        ),
        request_id=None,
        retry_after=None,
    )

    assert isinstance(error, EmailNotScheduledError)
    assert error.details["status"] == "CANCELED"


def test_unknown_code_degrades_to_status_class() -> None:
    """Новый код на бэкенде не должен ломать старый SDK."""
    error = error_from_response(
        status_code=404,
        body=_envelope("brand_new_code_from_the_future"),
        request_id=None,
        retry_after=None,
    )

    assert isinstance(error, NotFoundError)
    assert error.code == "brand_new_code_from_the_future"


def test_details_code_wins_over_envelope_code() -> None:
    """Секундный лимит приходит как `details.code = rps_exceeded`."""
    error = error_from_response(
        status_code=429,
        body=_envelope("rate_limit_exceeded", details={"code": "rps_exceeded"}),
        request_id=None,
        retry_after=1,
    )

    assert isinstance(error, RateLimitError)
    assert error.code == "rps_exceeded"
    assert error.retry_after == 1


def test_non_json_body_still_produces_typed_error() -> None:
    """Прокси перед API умеет отвечать HTML — SDK не должен падать на разборе."""
    error = error_from_response(
        status_code=502,
        body="<html><body>502 Bad Gateway</body></html>",
        request_id="req-proxy",
        retry_after=None,
    )

    assert isinstance(error, ServerError)
    assert error.code == "unknown_error"
    assert error.request_id == "req-proxy"


def test_error_message_is_readable() -> None:
    error = error_from_response(
        status_code=422,
        body=_envelope("domain_not_verified", message="Домен ещё не подтверждён."),
        request_id=None,
        retry_after=None,
    )

    text = str(error)
    assert "Домен ещё не подтверждён." in text
    assert "domain_not_verified" in text
    assert "status=422" in text


def test_client_raises_typed_error_from_api(client: MailInfra) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/emails").mock(
            return_value=httpx.Response(
                422,
                json=_envelope("domain_not_verified", "Домен ещё не подтверждён."),
                headers={"X-Request-ID": "req-header"},
            )
        )
        with pytest.raises(DomainNotVerifiedError) as excinfo:
            client.emails.send(
                from_email="hello@unverified.example.ru",
                to="user@example.com",
                subject="Тема",
                text="Текст",
            )

    assert excinfo.value.request_id == "req-header"


def test_malformed_success_response_is_reported(client: MailInfra) -> None:
    """Успешный ответ без `data` — тоже ошибка контракта, а не KeyError."""
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/emails").mock(return_value=httpx.Response(200, json={"items": []}))
        with pytest.raises(MailInfraError, match="не по контракту"):
            client.emails.list()


def test_rejects_key_with_unknown_prefix() -> None:
    with pytest.raises(ValueError, match="mi_live_"):
        MailInfra(api_key="sk_live_whatever")


def test_detects_environment_from_key_prefix() -> None:
    assert MailInfra(api_key=TEST_API_KEY).environment == "test"
    assert MailInfra(api_key=LIVE_API_KEY).environment == "live"
    # Legacy-ключи с заглавным префиксом бэкенд принимает — принимаем и мы.
    assert MailInfra(api_key="mi_LIVE_old0123456789abcdef").environment == "live"


def test_require_environment_guards_against_wrong_key() -> None:
    with pytest.raises(ValueError, match="live"):
        MailInfra(api_key=TEST_API_KEY, require_environment="live")


def test_api_key_is_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAILINFRA_API_KEY", LIVE_API_KEY)
    client = MailInfra()

    assert client.environment == "live"


def test_missing_api_key_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAILINFRA_API_KEY", raising=False)
    with pytest.raises(ValueError, match="MAILINFRA_API_KEY"):
        MailInfra()
