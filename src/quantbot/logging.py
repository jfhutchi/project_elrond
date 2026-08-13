"""Structured JSON logging with recursive secret redaction."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import TextIO

import structlog
from structlog.typing import EventDict, Processor

REDACTED = "[REDACTED]"
SECRET_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "secret",
        "token",
        "authorization",
        "password",
    }
)


def _redact_value(value: object, registered_secrets: frozenset[str]) -> object:
    if isinstance(value, str) and value in registered_secrets:
        return REDACTED
    if isinstance(value, Mapping):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and key.casefold() in SECRET_KEYS
                else _redact_value(nested, registered_secrets)
            )
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(nested, registered_secrets) for nested in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(nested, registered_secrets) for nested in value)
    return value


def redact_secrets(
    _logger: object,
    _method_name: str,
    event_dict: EventDict,
    *,
    registered_secrets: Collection[str] = (),
) -> EventDict:
    """Return a deeply copied event dictionary with sensitive values removed."""
    secrets = frozenset(secret for secret in registered_secrets if secret)
    redacted = _redact_value(event_dict, secrets)
    if not isinstance(redacted, dict):
        raise TypeError("structlog event dictionary must be a dictionary")
    return redacted


class SecretRedactor:
    """A reusable structlog processor carrying runtime-registered secrets."""

    def __init__(self, registered_secrets: Collection[str] = ()) -> None:
        self._registered_secrets = tuple(registered_secrets)

    def __call__(
        self,
        logger: object,
        method_name: str,
        event_dict: EventDict,
    ) -> EventDict:
        return redact_secrets(
            logger,
            method_name,
            event_dict,
            registered_secrets=self._registered_secrets,
        )


def configure_logging(
    *,
    stream: TextIO | None = None,
    registered_secrets: Collection[str] = (),
) -> None:
    """Configure structlog to emit timestamped JSON with mandatory redaction."""
    processors: list[Processor] = [
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        SecretRedactor(registered_secrets),
        structlog.processors.JSONRenderer(sort_keys=True),
    ]
    structlog.configure(
        processors=processors,
        logger_factory=structlog.PrintLoggerFactory(file=stream),
        wrapper_class=structlog.make_filtering_bound_logger(0),
        cache_logger_on_first_use=False,
    )
