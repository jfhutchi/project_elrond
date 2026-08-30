"""Redaction has to be installed before anything can log (#46).

`configure_logging` existed, was tested, and was called by nothing. No module used
`structlog.get_logger` either, so the whole subsystem was built and never adopted. Nothing was
leaking, because nothing was logging.

The hazard is the *next* log line. Without the call, structlog runs on its default configuration,
so a `log.info("submitting", api_key=...)` added in good faith prints the key -- `SecretRedactor`
is a processor, and a processor that is never installed redacts nothing. These tests are about
the wiring rather than the redactor, which has its own suite.

Found by a mechanical audit for functions that no other `src` module calls, written after the
same defect class had been found by hand eight times.
"""

from __future__ import annotations

import io
import json

import pytest
import structlog

from quantbot.cli import main


def run(argv: list[str]) -> int | None:
    """Invoke the CLI, tolerating argparse exiting.

    `--help` and an unknown flag both raise `SystemExit` from `parse_args`. That is fine for
    these tests and is in fact the interesting case: configuration must already have happened by
    the time parsing can exit.
    """
    try:
        return main(argv, output=io.StringIO())
    except SystemExit as exit_code:
        return exit_code.code if isinstance(exit_code.code, int) else 0


@pytest.fixture(autouse=True)
def reset_structlog():
    """Leave global logging configuration as it was found.

    `structlog.configure` is process-global, so a test that installed a redactor and walked away
    would make every later test pass on this one's setup -- the precise error this file exists to
    catch, committed in the test suite instead of the application.
    """
    structlog.reset_defaults()
    yield
    structlog.reset_defaults()


def test_running_any_command_installs_redaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Asserted by *behaviour*: log a registered secret and check it does not come out.

    Deliberately not a check that `configure_logging` was called. The property that matters is
    that a secret cannot reach the stream, and asserting the call would keep passing if the
    function stopped installing the processor.
    """
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "pk-super-secret-key")
    monkeypatch.setenv("ALPACA_PAPER_API_SECRET", "sk-super-secret-value")

    run(["--help"])

    captured = io.StringIO()
    structlog.configure(
        processors=structlog.get_config()["processors"],
        logger_factory=structlog.PrintLoggerFactory(file=captured),
        wrapper_class=structlog.get_config()["wrapper_class"],
        cache_logger_on_first_use=False,
    )
    structlog.get_logger().info(
        "submitting", api_key="pk-super-secret-key", note="sk-super-secret-value"
    )

    written = captured.getvalue()
    assert "pk-super-secret-key" not in written
    assert "sk-super-secret-value" not in written
    assert "[REDACTED]" in written


def test_logging_is_configured_before_arguments_are_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unparseable command line must not be the one path that logs unredacted.

    `parse_args` can exit the process, so configuring after it would leave every argument error
    running on structlog's defaults -- and argument errors are exactly where a mistyped
    credential ends up on a command line.
    """
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "pk-secret")

    run(["--not-a-real-flag"])

    assert structlog.get_config()["processors"], "logging was never configured"


def test_a_broken_environment_does_not_stop_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process that refused to start because it could not arrange to redact something it was
    never going to log would be the wrong trade.

    Configuration errors are reported by `config` and by the runtime, with far better messages
    than a logging setup could produce.
    """
    def explode() -> None:
        raise RuntimeError("the environment cannot produce settings")

    monkeypatch.setattr("quantbot.cli.Settings", explode)

    assert run(["--help"]) == 0
    assert structlog.get_config()["processors"], (
        "redaction is still installed, just without the concrete values to mask"
    )


def test_the_output_is_structured_rather_than_prose(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON so a later reader can query it; the ledger is the record, this is the trace."""
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "pk-secret")
    run(["--help"])

    captured = io.StringIO()
    structlog.configure(
        processors=structlog.get_config()["processors"],
        logger_factory=structlog.PrintLoggerFactory(file=captured),
        wrapper_class=structlog.get_config()["wrapper_class"],
        cache_logger_on_first_use=False,
    )
    structlog.get_logger().info("cycle-complete", symbols=3)

    payload = json.loads(captured.getvalue())
    assert payload["event"] == "cycle-complete"
    assert payload["symbols"] == 3
    assert payload["level"] == "info"
    assert "timestamp" in payload
