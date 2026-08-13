from __future__ import annotations

import json
from io import StringIO

import structlog
from pydantic import SecretStr

from quantbot.logging import REDACTED, SecretRedactor, configure_logging, redact_secrets


def test_redactor_recurses_without_mutating_the_caller_event() -> None:
    event = {
        "event": "broker request",
        "api_key": "key-123",
        "context": {
            "Authorization": "Bearer 123",
            "items": ["ordinary", "registered-value", {"password": "hunter2"}],
            "tuple": ("registered-value", "visible"),
        },
        "visible": 42,
    }

    redacted = redact_secrets(None, "info", event, registered_secrets=("registered-value",))

    assert redacted == {
        "event": "broker request",
        "api_key": REDACTED,
        "context": {
            "Authorization": REDACTED,
            "items": ["ordinary", REDACTED, {"password": REDACTED}],
            "tuple": (REDACTED, "visible"),
        },
        "visible": 42,
    }
    assert event["api_key"] == "key-123"
    assert event["context"]["Authorization"] == "Bearer 123"  # type: ignore[index]


def test_secret_redactor_processor_supports_registered_values() -> None:
    event = {"event": "test", "payload": {"token": "raw-token", "value": "exact-secret"}}

    result = SecretRedactor(("exact-secret",))(None, "info", event)

    assert result["payload"] == {"token": REDACTED, "value": REDACTED}
    assert event["payload"] == {"token": "raw-token", "value": "exact-secret"}


def test_configure_logging_emits_json_without_raw_secrets() -> None:
    output = StringIO()
    configure_logging(stream=output, registered_secrets=("registered-secret",))

    structlog.get_logger("quantbot.test").info(
        "broker request",
        api_secret="raw-api-secret",
        nested={"token": "raw-token", "value": "registered-secret", "safe": "kept"},
    )

    rendered = output.getvalue()
    payload = json.loads(rendered)
    assert payload["event"] == "broker request"
    assert payload["nested"]["safe"] == "kept"
    assert payload["api_secret"] == REDACTED
    assert payload["nested"]["token"] == REDACTED
    assert payload["nested"]["value"] == REDACTED
    for secret in ("raw-api-secret", "raw-token", "registered-secret"):
        assert secret not in rendered


def test_registered_secrets_are_redacted_inside_strings_and_exceptions() -> None:
    secret = "registered-secret"
    overlap = "registered-secret-suffix"
    protected = SecretStr("unknown-secret-object")
    error = RuntimeError(f"broker rejected {secret} twice: {secret}")
    event = {
        "event": f"request failed for {secret}",
        "callback_url": f"https://example.test/callback?credential={secret}",
        "header_value": f"Bearer {secret}",
        "nested": [
            f"prefix:{overlap}:{secret}:suffix",
            (error, {"message": f"embedded {secret}"}),
        ],
        "authorization": f"Bearer {secret}",
        "opaque": protected,
    }

    redacted = redact_secrets(
        None,
        "error",
        event,
        registered_secrets=(secret, overlap, ""),
    )

    rendered = repr(redacted)
    assert secret not in rendered
    assert overlap not in rendered
    assert redacted["event"] == f"request failed for {REDACTED}"
    assert redacted["callback_url"] == (
        f"https://example.test/callback?credential={REDACTED}"
    )
    assert redacted["header_value"] == f"Bearer {REDACTED}"
    assert redacted["nested"] == [
        f"prefix:{REDACTED}:{REDACTED}:suffix",
        (
            f"broker rejected {REDACTED} twice: {REDACTED}",
            {"message": f"embedded {REDACTED}"},
        ),
    ]
    assert redacted["authorization"] == REDACTED
    assert redacted["opaque"] is protected
    assert event["nested"][1][0] is error  # type: ignore[index]


def test_configured_json_logging_redacts_registered_secret_substrings() -> None:
    output = StringIO()
    secret = "url-secret"
    configure_logging(stream=output, registered_secrets=(secret,))

    structlog.get_logger("quantbot.test").error(
        f"request with {secret} failed",
        exception=RuntimeError(f"connection URL contained {secret}/{secret}"),
        nested={"url": f"https://example.test/{secret}"},
    )

    rendered = output.getvalue()
    payload = json.loads(rendered)
    assert secret not in rendered
    assert payload["event"] == f"request with {REDACTED} failed"
    assert payload["exception"] == f"connection URL contained {REDACTED}/{REDACTED}"
    assert payload["nested"]["url"] == f"https://example.test/{REDACTED}"
