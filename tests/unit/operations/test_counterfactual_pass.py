"""The scheduled pass that answers what a refusal cost (#57).

`evaluate_counterfactual` and `record_counterfactual_outcome` were both built, both tested, and
**neither had a caller**. The blocker on #57 was recorded as "no horizons have elapsed", which was
true and hid the fact that the pass did not exist -- so even once they elapsed, nothing would
have evaluated anything. Time was only half of it.

This is the eighth instance in this project of a control correct in isolation and never reached.
It is the one with the longest fuse: the others were wrong immediately, and this one would have
looked correct right up until the day it was supposed to start producing evidence.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from quantbot.domain import Bar
from quantbot.operations.runners import AdaptiveMomentumRunner
from quantbot.risk.counterfactual import HypotheticalExecutionPolicy
from quantbot.risk.shadow import (
    CandidateDirection,
    CounterfactualState,
    RejectedCandidate,
    RejectionReason,
    as_rows,
)
from quantbot.storage import Database, StorageRepository

DECISION = date(2026, 3, 2)
SESSIONS = [DECISION + timedelta(days=index) for index in range(60)]
NOW = datetime(2026, 8, 30, 20, 5, tzinfo=UTC)


def sessions(count: int) -> list[SimpleNamespace]:
    return [SimpleNamespace(session_date=day) for day in SESSIONS[:count]]


def bars(symbol: str, start: str, step: str = "1", count: int = 60) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            timestamp=datetime.combine(SESSIONS[index], datetime.min.time(), tzinfo=UTC),
            open=Decimal(start),
            high=Decimal(start) + Decimal(step) * index,
            low=Decimal(start),
            close=Decimal(start) + Decimal(step) * index,
            volume=Decimal("1000000"),
            adjustment=Decimal("1"),
        )
        for index in range(count)
    ]


def refusal(observation_id: str = "obs-1", symbol: str = "AAPL") -> RejectedCandidate:
    return RejectedCandidate(
        observation_id=observation_id,
        candidate_id=f"cand-{observation_id}",
        strategy_id="adaptive-momentum",
        configuration_hash="cfg-abc",
        symbol=symbol,
        direction=CandidateDirection.LONG,
        observed_at=datetime.combine(DECISION, datetime.min.time(), tzinfo=UTC),
        session_date=DECISION,
        reference_price=Decimal("100"),
        reasons=(
            RejectionReason(
                gate_id="CORRELATION_CONCENTRATION",
                gate_version=1,
                rule_parameters_hash="veto-policy-v1-aaaa",
                detail="would exceed the cluster ceiling",
            ),
        ),
    )


def runner(database: Database, **overrides: object) -> SimpleNamespace:
    fields: dict[str, object] = {
        "_database": database,
        "_identity": SimpleNamespace(
            strategy_id="adaptive-momentum", configuration_hash="cfg-abc"
        ),
        "_counterfactual": HypotheticalExecutionPolicy(),
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def evaluate(database: Database, calendar, **overrides: object) -> int:
    with database.transaction() as session:
        return AdaptiveMomentumRunner._evaluate_matured_counterfactuals(  # noqa: SLF001
            runner(database, **overrides), StorageRepository(session), calendar, now=NOW
        )


@pytest.fixture
def database(tmp_path: Path):
    db = Database.create(tmp_path / "counterfactual.db")
    try:
        yield db
    finally:
        db.close()


def seed(database: Database, *, symbol: str = "AAPL", benchmark: bool = True) -> None:
    parent, reasons = as_rows(refusal(symbol=symbol))
    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.record_rejected_candidate(parent, reasons)
        repository.save_bars(bars(symbol, "100"), provider="test")
        if benchmark:
            repository.save_bars(bars("SPY", "200", "0.5"), provider="test")


def outcomes(database: Database) -> list[dict[str, object]]:
    with database.transaction() as session:
        return [dict(row) for row in StorageRepository(session).list_counterfactual_outcomes()]


# --- not yet is never recorded as an answer ----------------------------------------------------


def test_a_horizon_that_has_not_elapsed_writes_nothing(database: Database) -> None:
    """The property that keeps the table honest while it fills.

    `UNAVAILABLE` means the horizon has not run. Storing it would record a non-answer as an
    answer, making "not yet" indistinguishable from "measured" for every reader downstream --
    and this table exists precisely to be counted.
    """
    seed(database)

    answered = evaluate(database, sessions(5))

    assert answered == 0
    assert outcomes(database) == []


def test_an_elapsed_horizon_is_answered_and_stored(database: Database) -> None:
    """The whole point, and the thing that had no caller."""
    seed(database)

    answered = evaluate(database, sessions(40))

    stored = outcomes(database)
    assert answered == 1
    assert len(stored) == 1
    assert stored[0]["state"] == CounterfactualState.EVALUATED.value
    assert stored[0]["observation_id"] == "obs-1"
    assert stored[0]["net_excess_bps"] is not None


def test_an_answered_refusal_is_not_evaluated_again(database: Database) -> None:
    """The pass runs every cycle, so it must not redo work that accumulates.

    Asserted on the *work*, not on the row count. The repository is idempotent by outcome id, so
    a count-only assertion passes whether or not the refusal was skipped -- which is how the
    first version of this test hid a filter keyed on `rejected_candidates.state`, a column that
    never advances. Left in, every historical refusal would have been re-evaluated on every
    cycle, at a cost growing with exactly the history this is meant to accumulate.
    """
    seed(database)
    evaluate(database, sessions(40))

    reads: list[str] = []

    class Counting(StorageRepository):
        def list_bars(self, **kwargs):  # type: ignore[no-untyped-def]
            reads.append(str(kwargs.get("symbol")))
            return super().list_bars(**kwargs)

    with database.transaction() as session:
        answered = AdaptiveMomentumRunner._evaluate_matured_counterfactuals(  # noqa: SLF001
            runner(database), Counting(session), sessions(40), now=NOW
        )

    assert answered == 0
    assert len(outcomes(database)) == 1
    assert "AAPL" not in reads, "the answered refusal was re-read and re-evaluated"


def test_a_different_policy_re_evaluates_rather_than_inheriting(database: Database) -> None:
    """A different execution assumption is a different dataset.

    Skipping on the strength of an outcome computed under another policy would silently attribute
    one policy's answer to another -- the pooling `HypotheticalExecutionPolicy.identity` exists to
    prevent.
    """
    seed(database)
    evaluate(database, sessions(40))
    strict = HypotheticalExecutionPolicy(round_trip_cost_bps=Decimal("300"))

    answered = evaluate(database, sessions(40), _counterfactual=strict)

    stored = outcomes(database)
    assert answered == 1
    assert len(stored) == 2
    assert {str(row["policy_identity"]) for row in stored} == {
        HypotheticalExecutionPolicy().identity,
        strict.identity,
    }


# --- what it may not do ------------------------------------------------------------------------


def test_the_outcome_names_the_policy_it_was_computed_under(database: Database) -> None:
    """Two outcomes under different execution assumptions are two datasets.

    Choosing the assumption after seeing returns is how a hypothetical fill becomes a convenient
    one, so the policy is an input to evaluation and travels with every result.
    """
    seed(database)
    strict = HypotheticalExecutionPolicy(horizon_sessions=10, round_trip_cost_bps=Decimal("50"))

    evaluate(database, sessions(40), _counterfactual=strict)

    assert outcomes(database)[0]["policy_identity"] == strict.identity
    assert strict.identity != HypotheticalExecutionPolicy().identity


def test_a_missing_benchmark_is_terminal_rather_than_a_raw_return(database: Database) -> None:
    """A raw return would credit the refusal for market direction it had no view on."""
    seed(database, benchmark=False)

    evaluate(database, sessions(40))

    stored = outcomes(database)
    assert len(stored) == 1
    assert stored[0]["state"] == CounterfactualState.UNEVALUABLE.value
    assert stored[0]["net_excess_bps"] is None
    assert "BENCHMARK" in str(stored[0]["unevaluable_reason"])


def test_an_empty_calendar_answers_nothing_rather_than_guessing(database: Database) -> None:
    """A horizon counted in sessions needs sessions; without them nothing can be placed."""
    seed(database)

    assert evaluate(database, []) == 0
    assert outcomes(database) == []


def test_prices_are_read_from_storage_rather_than_the_cycles_own_window(
    database: Database,
) -> None:
    """A horizon reaches back further than a decision window.

    An evaluator restricted to what today's cycle happened to read would silently skip exactly
    the older refusals it exists to answer -- and would do so without erroring, leaving the table
    quietly incomplete.
    """
    seed(database)

    # Nothing resembling a cycle window is passed in; only the calendar and the store.
    assert evaluate(database, sessions(40)) == 1


def test_each_symbols_prices_are_fetched_once(database: Database) -> None:
    """Two refusals on one symbol must not mean two full price reads.

    Not a micro-optimisation: the pass runs inside the trading cycle, and a per-refusal read
    would make its cost grow with the history it is meant to accumulate.
    """
    parent, reasons = as_rows(refusal(observation_id="obs-2"))
    seed(database)
    with database.transaction() as session:
        StorageRepository(session).record_rejected_candidate(parent, reasons)

    reads: list[str] = []

    class Counting(StorageRepository):
        def list_bars(self, **kwargs):  # type: ignore[no-untyped-def]
            reads.append(str(kwargs.get("symbol")))
            return super().list_bars(**kwargs)

    with database.transaction() as session:
        answered = AdaptiveMomentumRunner._evaluate_matured_counterfactuals(  # noqa: SLF001
            runner(database), Counting(session), sessions(40), now=NOW
        )

    assert answered == 2
    assert reads.count("AAPL") == 1, "one read for two refusals on the same symbol"


def test_the_cycle_actually_runs_the_pass() -> None:
    """The guard against a ninth instance, asserted structurally.

    Every test above exercises `_evaluate_matured_counterfactuals` directly and would pass
    unchanged if nothing in the cycle called it -- which is exactly the state #57 was in before
    this commit, with a green suite and no caller. No unit test of a component can see whether
    anything invokes it, so the call site is asserted over the source.
    """
    import ast

    tree = ast.parse(Path("src/quantbot/operations/runners.py").read_text(encoding="utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "_evaluate_matured_counterfactuals" in called, (
        "the evaluator has no caller, so no refusal would ever be answered"
    )
