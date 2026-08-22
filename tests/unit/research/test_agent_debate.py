"""TradingAgents behind the worker boundary (#37).

The framework was run for real (pinned `a33fd4c0`, own venv, local Ollama) before any of this
was written, and three measurements from that run are what these tests defend:

* its data layer fails open -- Reddit returned 429 twice and it reported anyway;
* it is not reproducible -- two identical invocations returned `Hold` then `Buy`;
* the trading decision is the most prominent thing it returns.

So the properties under test are: the decision cannot cross the boundary, the debate arrives
marked as derived and unverifiable, and it becomes *material* rather than an analysis.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from quantbot.research.agent_debate import (
    TRADINGAGENTS_REVISION,
    TradingAgentsSourceProvider,
    TradingAgentsUnavailable,
    worker_environment,
)
from quantbot.research.agent_debate_worker import (
    FORBIDDEN_KEYS,
    WorkerRefused,
    encode,
    extract,
)
from quantbot.research.sources import DerivedArtifactCited, SourceHealth, SourceKind

NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)

#: Shaped like the real graph state, with the field names taken from an actual run.
STATE = {
    "company_of_interest": "SPY",
    "final_trade_decision": "Buy",
    "trader_investment_plan": "accumulate on weakness",
    "investment_debate_state": {
        "bull_history": "Bull Analyst: earnings breadth is widening across sectors",
        "bear_history": "Bear Analyst: the P/E of 23.2 sits above its long-run average",
        "judge_decision": "on balance the bull case is stronger",
        "count": 1,
    },
    "risk_debate_state": {
        "aggressive_history": "Aggressive: size up while momentum persists",
        "conservative_history": "Conservative: drawdown risk is understated here",
        "neutral_history": "Neutral: the disagreement is about horizon, not direction",
        "judge_decision": "moderate sizing",
    },
}


def payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "ticker": "SPY",
        "trade_date": "2026-08-12",
        "revision": TRADINGAGENTS_REVISION,
        "debate": extract(dict(STATE)),
    }
    base.update(overrides)
    return base


def test_the_trading_decision_cannot_cross_the_boundary() -> None:
    """The single property this integration exists to guarantee.

    `propagate()` returns the decision as its second value and `final_trade_decision` sits at
    the top level of state, so a caller that forgot to strip it would be the normal case rather
    than the exceptional one. It is dropped in the worker, before the process boundary.
    """
    carried = extract(dict(STATE))

    assert set(carried) == {
        "bull_history",
        "bear_history",
        "aggressive_history",
        "conservative_history",
        "neutral_history",
    }
    for forbidden in FORBIDDEN_KEYS:
        assert forbidden not in carried
        assert not any(forbidden in text for text in carried.values())


def test_the_judge_decision_is_dropped_because_it_resolves_the_disagreement() -> None:
    """Preserved disagreement is the output worth having (#37).

    A synthesis that resolves the bull and bear into one view destroys the only part a critic
    can act on, so the judge's verdict is excluded even though it is not a trade instruction.
    """
    carried = extract(dict(STATE))

    assert "on balance the bull case is stronger" not in " ".join(carried.values())
    assert carried["bull_history"] and carried["bear_history"]


def test_a_graph_that_produced_no_debate_is_refused_not_returned_empty() -> None:
    """An empty bundle would read as "the agents considered it and had nothing to say".

    That is a claim about the hypothesis. What actually happened is a claim about the run.
    """
    with pytest.raises(WorkerRefused, match="no bull or bear debate"):
        extract({"investment_debate_state": {}, "risk_debate_state": {}})


def test_the_debate_arrives_as_derived_and_unverifiable_material() -> None:
    """Both flags are measurements, not caution.

    `derived` because it is an LLM artifact produced from sources; `Source` then refuses to let
    it be cited as evidence. `UNVERIFIABLE` because the framework's fetchers fail open -- it
    reported through two HTTP 429s -- so this project cannot establish what it actually read.
    """
    sources = TradingAgentsSourceProvider.to_sources(payload(), now=NOW)

    assert len(sources) == 5
    for source in sources:
        assert source.derived is True
        assert source.derived_from == f"tradingagents@{TRADINGAGENTS_REVISION}"
        assert source.health is SourceHealth.UNVERIFIABLE
        assert source.kind is SourceKind.ALTERNATIVE
        assert TRADINGAGENTS_REVISION in source.uri


def test_a_debate_cannot_be_cited_as_evidence() -> None:
    """The standing an LLM debate deserves, enforced by the type rather than by a convention."""
    from quantbot.research.sources import EpistemicStatus, cite  # noqa: PLC0415

    source = TradingAgentsSourceProvider.to_sources(payload(), now=NOW)[0]

    with pytest.raises(DerivedArtifactCited, match="derived from"):
        cite(
            source,
            claim="the bull case is stronger",
            locator="bull_history",
            status=EpistemicStatus.UNTESTED,
        )


def test_the_debate_is_dated_to_the_session_it_discusses_not_to_now() -> None:
    """`retrieved_at` is when we ran it; `published_at` is what it is about.

    Using `now` for both would let a backtest read a debate about 2026-08-12 while standing
    earlier than that date.
    """
    source = TradingAgentsSourceProvider.to_sources(payload(), now=NOW)[0]

    assert source.published_at == datetime(2026, 8, 12, tzinfo=UTC)
    assert source.retrieved_at == NOW


def test_a_different_framework_revision_is_refused() -> None:
    """ "TradingAgents said so" is not provenance unless it says which TradingAgents."""
    with pytest.raises(TradingAgentsUnavailable, match="different framework revision"):
        TradingAgentsSourceProvider.to_sources(payload(revision="0" * 40), now=NOW)


def test_a_payload_with_no_debate_or_no_subject_is_refused() -> None:
    with pytest.raises(TradingAgentsUnavailable, match="carries no debate"):
        TradingAgentsSourceProvider.to_sources(payload(debate={}), now=NOW)
    with pytest.raises(TradingAgentsUnavailable, match="ticker and date"):
        TradingAgentsSourceProvider.to_sources(payload(ticker=""), now=NOW)


def test_identical_debates_get_identical_ids_and_a_revision_gets_a_new_one() -> None:
    """Content-addressed, so a reworded debate is a different record rather than the same one.

    It matters more here than elsewhere: the framework is not reproducible, so two runs of the
    same question legitimately differ and must not silently overwrite each other.
    """
    first = TradingAgentsSourceProvider.to_sources(payload(), now=NOW)
    again = TradingAgentsSourceProvider.to_sources(payload(), now=NOW)
    reworded = dict(extract(dict(STATE)), bull_history="Bull Analyst: something else entirely")
    changed = TradingAgentsSourceProvider.to_sources(payload(debate=reworded), now=NOW)

    assert [s.source_id for s in first] == [s.source_id for s in again]
    assert {s.source_id for s in changed} != {s.source_id for s in first}


def test_the_child_sees_a_local_endpoint_and_no_credentials() -> None:
    """Third-party code that opens sockets must not inherit this project's keys.

    The endpoint is pinned rather than defaulted for the same reason: a framework that silently
    reached a hosted provider would spend money nobody authorised and send this project's
    research questions to a third party.
    """
    parent = {
        "ALPACA_API_KEY": "must-not-cross",
        "ALPACA_PAPER_API_SECRET": "must-not-cross",
        "QUANTBOT_DB_PATH": "must-not-cross",
        "OPENAI_API_KEY": "a-real-key-that-must-not-be-used",
        "PATH": "/usr/bin",
    }

    environment = worker_environment(
        parent, endpoint="http://localhost:11434/v1", model="mistral:7b", cache_directory=Path(".")
    )

    assert not any(key.startswith(("ALPACA_", "QUANTBOT_")) for key in environment)
    assert environment["OPENAI_API_KEY"] == "local-endpoint-no-key-required"
    assert environment["TRADINGAGENTS_LLM_BACKEND_URL"] == "http://localhost:11434/v1"
    assert environment["PATH"] == "/usr/bin"


def test_an_interpreter_inside_the_elrond_tree_is_refused(tmp_path: Path) -> None:
    """The boundary is the only thing making it acceptable to run this code at all."""
    with pytest.raises(ValueError, match="outside the Elrond tree"):
        TradingAgentsSourceProvider(
            python_executable=Path(__file__).resolve(),
            cache_directory=tmp_path,
            endpoint="http://localhost:11434/v1",
            model="mistral:7b",
        )


def test_the_payload_guard_refuses_a_conclusion_that_survived_extraction() -> None:
    """The last line of defence, and it was unreachable by any test until now.

    A mutation that reintroduced the decision passed the entire suite, because every test
    exercised `extract` and nothing exercised `main` -- so the guard existed and was never asked
    to guard anything. That is the same shape as the three tests this session that passed while
    measuring nothing.

    The scan is on the serialised payload rather than on keys, because the leak that matters is
    a conclusion embedded in a debate string, not a tidy extra field.
    """
    clean = {"ticker": "SPY", "debate": {"bull_history": "breadth is widening"}}
    assert "bull_history" in encode(clean)

    for forbidden in FORBIDDEN_KEYS:
        leaked = {"ticker": "SPY", "debate": {"bull_history": f"see {forbidden} above"}}
        with pytest.raises(WorkerRefused, match="the boundary leaked"):
            encode(leaked)
