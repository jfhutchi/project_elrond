"""Model-authored experiment code, and every way its output could be a fabrication (#8, #21)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from quantbot.research.codegen import (
    GeneratedCode,
    GenerationRefused,
    run_generated_signal,
)
from quantbot.sandbox.runner import SandboxResult

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

BARS = {
    "SPY": [
        {"date": "2026-08-17", "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1"},
        {"date": "2026-08-18", "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1"},
    ]
}


class FakeSandbox:
    def __init__(self, stdout: str, *, os_enforced: bool = True, **overrides: object) -> None:
        self.os_enforced = os_enforced
        self.ran: list[str] = []
        self._result = SandboxResult(
            ok=overrides.get("ok", True),  # type: ignore[arg-type]
            exit_code=overrides.get("exit_code", 0),  # type: ignore[arg-type]
            stdout=stdout,
            stderr=overrides.get("stderr", ""),  # type: ignore[arg-type]
            duration_seconds=2.0,
            terminated_reason=overrides.get("terminated_reason"),  # type: ignore[arg-type]
        )

    def run(self, source: str) -> SandboxResult:
        self.ran.append(source)
        return self._result


def code(source: str = "print('hi')") -> GeneratedCode:
    import hashlib

    class _Response:
        spec = type("S", (), {"model": "coder", "parameters_hash": "p"})()

        def provenance(self):
            return type(
                "P", (), {"model": "coder@1", "prompt_template_hash": "t" * 8}
            )()

    return GeneratedCode(
        source=source,
        sha256=hashlib.sha256(source.encode()).hexdigest(),
        response=_Response(),  # type: ignore[arg-type]
    )


def emitting(signal: dict) -> str:
    return "working...\n" + json.dumps({"signal": signal})


def test_generated_code_is_refused_a_sandbox_that_only_conceals() -> None:
    """Model-authored code is the strongest case for the kernel-isolation rule.

    Nobody reviewed this source. Under the Windows backend an absolute path still resolves to
    the dotenv and the durable ledger, and a signal computed with that access would look exactly
    like one computed without it.
    """
    sandbox = FakeSandbox(emitting({"2026-08-17": {"SPY": "1"}}), os_enforced=False)

    with pytest.raises(GenerationRefused, match="OS-enforced sandbox"):
        run_generated_signal(sandbox, code(), BARS)

    assert sandbox.ran == [], "nothing should have been executed"


def test_a_stopped_run_produces_nothing() -> None:
    """A signal from a killed script covers an unknown fraction of the data.

    Reading it as a result is the same error as treating a worker timeout as a search that
    found nothing.
    """
    sandbox = FakeSandbox(
        emitting({"2026-08-17": {"SPY": "1"}}), ok=False, terminated_reason="wall-clock"
    )

    with pytest.raises(GenerationRefused, match="unknown fraction"):
        run_generated_signal(sandbox, code(), BARS)


def test_a_signal_for_a_date_that_was_not_supplied_is_refused() -> None:
    """A weight for a date the script was not given did not come from the data.

    Either it was hallucinated or it came from somewhere the script should not have reached.
    Refused rather than trimmed to the dates that line up: silently discarding rows would hide
    whichever of those two happened.
    """
    sandbox = FakeSandbox(emitting({"2027-01-04": {"SPY": "1"}}))

    with pytest.raises(GenerationRefused, match="not among the bars supplied"):
        run_generated_signal(sandbox, code(), BARS)


def test_a_signal_naming_a_symbol_outside_the_universe_is_refused() -> None:
    """The universe is part of the pre-registration. Trading outside it is a different claim."""
    sandbox = FakeSandbox(emitting({"2026-08-17": {"TSLA": "1"}}))

    with pytest.raises(GenerationRefused, match="outside the universe"):
        run_generated_signal(sandbox, code(), BARS)


def test_a_run_that_printed_no_signal_cannot_be_evaluated() -> None:
    sandbox = FakeSandbox("computed some things, looked promising")

    with pytest.raises(GenerationRefused, match="printed no signal"):
        run_generated_signal(sandbox, code(), BARS)


def test_the_generated_code_cannot_choose_where_its_inputs_come_from() -> None:
    """The input preamble is written by this module, never by the model.

    A script that could choose its own input path could choose one outside the workspace, and
    the entire point of copying data in is that it is the only data there is.
    """
    sandbox = FakeSandbox(emitting({"2026-08-17": {"SPY": "0.5"}}))

    run_generated_signal(sandbox, code("print('mine')"), BARS)

    executed = sandbox.ran[0]
    assert executed.startswith("import json as _json, pathlib as _pathlib")
    assert "inputs/bars.json" in executed
    # The model's own source is present unmodified, after the preamble.
    assert executed.rstrip().endswith("print('mine')")


def test_the_run_carries_everything_it_emitted() -> None:
    """#18 asks that generated-code output be connected to the result bundle automatically.

    Reconstructing it later from logs assumes somebody kept the logs, and the run that most
    needs explaining is the one nobody expected to have to explain.
    """
    sandbox = FakeSandbox(
        emitting({"2026-08-18": {"SPY": "0.25"}}), stderr="a warning nobody expected"
    )

    run = run_generated_signal(sandbox, code(), BARS)

    assert run.signal == {"2026-08-18": {"SPY": "0.25"}}
    assert run.stderr == "a warning nobody expected"
    assert "working..." in run.stdout
    assert run.code.sha256 == code().sha256
    assert run.code.provenance["code_sha256"] == run.code.sha256
    assert run.duration_seconds == 2.0
