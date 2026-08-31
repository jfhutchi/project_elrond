"""Whether a host is configured to run, per capability (#150, #82).

`quantbot config` inventories settings. It never answers the question somebody standing up a host
actually has -- is this configured enough to work -- and that answer was spread across forty JSON
rows a reader had to already understand.

The capability split is the design. This machine has broker credentials and no model
configuration, so it can trade and cannot research, which is exactly why the paper account has
fills while every research table is empty. A single ready/not-ready flag would have to be wrong
about one of those.
"""

from __future__ import annotations

import io
import json

import pytest

from quantbot.operations.preflight import Capability, preflight, unready
from quantbot.research.composition import CRITIC, ENDPOINT, GENERATOR

BROKER = ("ALPACA_PAPER_API_KEY", "ALPACA_PAPER_API_SECRET")


def checks(**environ: str) -> dict[Capability, object]:
    """Preflight over an explicit environment, so the answer is not a property of the shell."""
    return {check.capability: check for check in preflight(environ)}


def fully_configured() -> dict[str, str]:
    return {
        "ALPACA_PAPER_API_KEY": "key",
        "ALPACA_PAPER_API_SECRET": "secret",
        ENDPOINT: "http://localhost:11434/v1",
        GENERATOR: "qwen2.5-coder:7b",
        CRITIC: "mistral:7b",
    }


def test_a_host_can_be_ready_to_trade_and_not_to_research() -> None:
    """The split this exists for, and the state this machine is actually in.

    Broker credentials present, no model configuration. Reporting one verdict would have to call
    that either ready -- hiding why research produces nothing -- or not ready, which would say the
    paper account cannot trade when it demonstrably has.
    """
    result = checks(ALPACA_PAPER_API_KEY="key", ALPACA_PAPER_API_SECRET="secret")

    assert result[Capability.PAPER_TRADING].ready is True  # type: ignore[union-attr]
    assert result[Capability.MARKET_DATA].ready is True  # type: ignore[union-attr]
    assert result[Capability.RESEARCH].ready is False  # type: ignore[union-attr]


def test_missing_variables_are_named() -> None:
    """"Research is not configured" sends somebody to read source; the names do not."""
    research = checks()[Capability.RESEARCH]

    assert set(research.missing) == {ENDPOINT, GENERATOR, CRITIC}  # type: ignore[union-attr]


def test_the_consequence_is_stated_rather_than_left_to_be_inferred() -> None:
    """An operator needs to know what stops working, not only which variable is unset.

    The research consequence is the one that matters most here: every research table stays empty
    for a reason that is *not* "nothing was found", and that distinction is the whole reason this
    project could be misread as having searched and failed.
    """
    result = checks()

    assert "MARKET_DATA_STALE" in result[Capability.MARKET_DATA].consequence  # type: ignore[union-attr]
    assert "nothing reconciles" in result[Capability.PAPER_TRADING].consequence  # type: ignore[union-attr]
    assert "not 'nothing was found'" in result[Capability.RESEARCH].consequence  # type: ignore[union-attr]


def test_one_model_for_both_roles_is_a_refusal_not_a_missing_variable() -> None:
    """Every variable is set and it still cannot run.

    Reporting a missing-variable list here would send someone hunting for something already
    there. The rule itself stands: a model asked to review its own proposal tends to find it
    sound.
    """
    environ = fully_configured() | {GENERATOR: "mistral:7b", CRITIC: "mistral:7b"}

    research = checks(**environ)[Capability.RESEARCH]

    assert research.ready is False  # type: ignore[union-attr]
    assert research.missing == ()  # type: ignore[union-attr]
    assert "same model" in research.refusal  # type: ignore[union-attr]


def test_a_fully_configured_host_reports_nothing_unready() -> None:
    """The state a provisioned VM should reach, and the gate that says it has."""
    assert unready(preflight(fully_configured())) == ()


def test_unready_names_the_capabilities_that_are_not_ready() -> None:
    """Asserting only the empty case is a tautology, which mutation testing caught.

    `unready` returning `()` unconditionally passed the fully-configured test, because that test
    expects `()` either way. A filter is only demonstrated by a case it has to filter.
    """
    blocked = unready(preflight({"ALPACA_PAPER_API_KEY": "k", "ALPACA_PAPER_API_SECRET": "s"}))

    assert [check.capability for check in blocked] == [Capability.RESEARCH]


def test_unready_names_every_unconfigured_capability_on_a_bare_host() -> None:
    """A host with nothing set is blocked on three things, not one."""
    blocked = unready(preflight({}))

    assert {check.capability for check in blocked} == {
        Capability.PAPER_TRADING,
        Capability.MARKET_DATA,
        Capability.RESEARCH,
    }
    assert Capability.CONSOLE not in {check.capability for check in blocked}


def test_the_console_needs_nothing_of_its_own() -> None:
    """Reporting it as blocked would suggest a dependency it does not have.

    The console reads the store; whether a store exists is `migrate-database`'s question.
    """
    assert checks()[Capability.CONSOLE].ready is True  # type: ignore[union-attr]


def test_whitespace_is_not_a_configured_credential() -> None:
    """A variable set to spaces is the classic half-provisioned host."""
    result = checks(ALPACA_PAPER_API_KEY="   ", ALPACA_PAPER_API_SECRET="secret")

    assert result[Capability.PAPER_TRADING].ready is False  # type: ignore[union-attr]
    assert "ALPACA_PAPER_API_KEY" in result[Capability.PAPER_TRADING].missing  # type: ignore[union-attr]


def test_nothing_reads_the_ambient_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passing an environment must mean *that* environment.

    Otherwise this could not be used to check a host other than the one it runs on, which is the
    only thing that makes it useful for provisioning rather than for self-description.
    """
    monkeypatch.setenv(ENDPOINT, "http://should-not-be-read")
    monkeypatch.setenv(GENERATOR, "leaked-generator")
    monkeypatch.setenv(CRITIC, "leaked-critic")

    research = checks()[Capability.RESEARCH]

    assert research.ready is False, "the ambient shell leaked into an explicit environment"  # type: ignore[union-attr]


def test_no_credential_value_is_ever_reported() -> None:
    """Names only. This output is meant to be pasteable into a public issue."""
    rendered = json.dumps(
        [
            {"missing": list(check.missing), "refusal": check.refusal}
            for check in preflight(fully_configured())
        ]
    )

    assert "key" not in rendered
    assert "secret" not in rendered
    assert "11434" not in rendered


def test_the_cli_exits_nonzero_while_anything_is_unready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Usable as a provisioning gate, not only as a report.

    The environment is pinned rather than inherited, and the expected exit code is stated rather
    than derived from the payload -- my first version asserted `code == (0 if not
    payload["unready"] else 1)`, which is true whatever the command does.
    """
    from quantbot.cli import main

    for name in (*BROKER, ENDPOINT, GENERATOR, CRITIC):
        monkeypatch.delenv(name, raising=False)

    stream = io.StringIO()
    code = main(["preflight"], output=stream)
    payload = json.loads(stream.getvalue())

    assert code == 1, "a bare host is not a provisioned one"
    assert payload["ok"] is False
    assert set(payload["unready"]) == {"PAPER_TRADING", "MARKET_DATA", "RESEARCH"}
    assert {item["capability"] for item in payload["capabilities"]} == {
        capability.value for capability in Capability
    }


def test_the_cli_exits_zero_on_a_configured_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half. Without it, always-exit-1 would pass the test above."""
    from quantbot.cli import main

    for name, value in fully_configured().items():
        monkeypatch.setenv(name, value)

    stream = io.StringIO()
    code = main(["preflight"], output=stream)

    assert code == 0
    assert json.loads(stream.getvalue())["unready"] == []
