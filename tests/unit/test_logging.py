from __future__ import annotations

import json
from io import StringIO

import structlog

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
