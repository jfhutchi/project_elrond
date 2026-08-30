"""What a refusal cost, and every way that number flatters the gate that produced it (#57).

This is the half of #57 where a research system most easily fools itself. A counterfactual is a
number nobody can check against reality, so almost every test here is a restriction: on when it
may be computed, on what it may be computed from, and on what it may say when it cannot be.
"""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.risk.counterfactual import (
    CounterfactualOutcome,
    HypotheticalExecutionPolicy,
    UnevaluableReason,
    evaluate_counterfactual,
    horizon_session_for,
)
from quantbot.risk.shadow import CandidateDirection, CounterfactualState

SOURCE = Path("src/quantbot/risk/counterfactual.py")
NOW = datetime(2026, 8, 30, 20, 5, tzinfo=UTC)
SESSIONS = [date(2026, 3, 2) + timedelta(days=index) for index in range(40)]


def rising(start: str, step: str = "1") -> dict[date, Decimal]:
    return {
        day: Decimal(start) + Decimal(step) * index for index, day in enumerate(SESSIONS)
    }


def policy(**overrides: object) -> HypotheticalExecutionPolicy:
    return HypotheticalExecutionPolicy(**overrides)  # type: ignore[arg-type]


def evaluate(**overrides: object) -> CounterfactualOutcome:
    fields: dict[str, object] = {
        "observation_id": "obs-1",
        "direction": CandidateDirection.LONG,
        "reference_price": Decimal("100"),
        "decision_session": SESSIONS[0],
        "policy": policy(),
        "sessions": SESSIONS,
        "symbol_closes": rising("100"),
        "benchmark_closes": rising("200", "0.5"),
        "evaluated_at": NOW,
    }
    fields.update(overrides)
    return evaluate_counterfactual(**fields)  # type: ignore[arg-type]


# --- the boundary -------------------------------------------------------------------------------


def test_a_hypothetical_outcome_cannot_reach_an_execution_path() -> None:
    """A counterfactual that could become an order eventually would."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)

    forbidden = {"broker", "execution", "coordinator", "OrderIntent", "OrderSide"}
    offenders = [name for name in imported if any(item in name for item in forbidden)]
    assert not offenders, f"the counterfactual path must not reach execution: {offenders}"


# --- the policy is frozen before the outcome -----------------------------------------------------


def test_the_policy_identity_covers_everything_that_moves_the_number() -> None:
    """Choosing an execution assumption after seeing returns is how a fill becomes convenient.

    Two outcomes under different policies are two datasets. Pooling them defeats the question
    #57 exists to ask, so the policy is an input to evaluation and never a parameter of it.
    """
    base = policy()

    assert base.identity.startswith("counterfactual-policy-v1-")
    for change in (
        {"horizon_sessions": 10},
        {"round_trip_cost_bps": Decimal("25")},
        {"benchmark_symbol": "QQQ"},
        {"version": 2},
    ):
        assert policy(**change).identity != base.identity, change


def test_every_outcome_names_the_policy_it_was_computed_under() -> None:
    """Without it, a number cannot be grouped and therefore cannot be trusted."""
    strict = policy(horizon_sessions=5)
    outcome = evaluate(policy=strict)

    assert outcome.policy_identity == strict.identity
    assert outcome.policy_identity != policy().identity


# --- when it may be computed ---------------------------------------------------------------------


def test_an_outcome_is_not_computed_before_its_horizon_elapses() -> None:
    """"Not yet" is a state, and evaluating early would be reading a horizon that has not run."""
    outcome = evaluate(decision_session=SESSIONS[-1])

    assert outcome.state is CounterfactualState.UNAVAILABLE
    assert outcome.net_excess_bps is None
    assert outcome.unevaluable_reason is None, "not yet is not the same as never"
    assert "has not elapsed" in outcome.detail


def test_the_horizon_is_counted_in_sessions_rather_than_days() -> None:
    """A calendar week and a trading week are different lengths (#74's lesson, reapplied).

    A horizon in days drifts across holidays, so two decisions made a week apart would be
    measured over different amounts of market activity.
    """
    sparse = [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 10), date(2026, 3, 11)]

    assert horizon_session_for(
        decision_session=sparse[0], sessions=sparse, horizon_sessions=2
    ) == date(2026, 3, 10)
    assert (
        horizon_session_for(decision_session=sparse[0], sessions=sparse, horizon_sessions=99)
        is None
    )


def test_an_elapsed_horizon_with_no_price_is_terminal_and_says_why() -> None:
    """Never, rather than not yet -- and never a zero, which would be a measurement."""
    gapped = {day: value for day, value in rising("100").items() if day != SESSIONS[20]}
    outcome = evaluate(symbol_closes=gapped)

    assert outcome.state is CounterfactualState.UNEVALUABLE
    assert outcome.unevaluable_reason is UnevaluableReason.HORIZON_PRICE_MISSING
    assert outcome.net_excess_bps is None
    assert outcome.horizon_session == SESSIONS[20]
    assert "stale number" in outcome.detail


def test_a_missing_benchmark_is_its_own_terminal_reason() -> None:
    """A raw return would credit the refusal for market direction it had no view on."""
    outcome = evaluate(benchmark_closes={})

    assert outcome.state is CounterfactualState.UNEVALUABLE
    assert outcome.unevaluable_reason is UnevaluableReason.BENCHMARK_PRICE_MISSING


# --- what it may say ------------------------------------------------------------------------------


def test_an_unevaluated_outcome_cannot_carry_a_number() -> None:
    """A quotable figure beside an "unavailable" label gets quoted."""
    with pytest.raises(ValueError, match="must not hold"):
        CounterfactualOutcome(
            observation_id="obs-1",
            policy_identity="counterfactual-policy-v1-x",
            state=CounterfactualState.UNAVAILABLE,
            net_excess_bps=Decimal("120"),
            detail="a return with no evaluation behind it",
        )


def test_a_terminal_outcome_must_say_why_it_is_terminal() -> None:
    """Otherwise it is indistinguishable from one nobody has got to yet."""
    with pytest.raises(ValueError, match="must say why"):
        CounterfactualOutcome(
            observation_id="obs-1",
            policy_identity="counterfactual-policy-v1-x",
            state=CounterfactualState.UNEVALUABLE,
            horizon_session=SESSIONS[20],
            evaluated_at=NOW,
            detail="terminal, unexplained",
        )


def test_not_yet_cannot_claim_a_reason_it_never_will_be() -> None:
    """`UNAVAILABLE` and `UNEVALUABLE` are different facts and must not blur."""
    with pytest.raises(ValueError, match="cannot carry a terminal reason"):
        CounterfactualOutcome(
            observation_id="obs-1",
            policy_identity="counterfactual-policy-v1-x",
            state=CounterfactualState.UNAVAILABLE,
            unevaluable_reason=UnevaluableReason.HORIZON_PRICE_MISSING,
            detail="not yet, but claiming never",
        )


def test_a_partial_evaluation_is_refused() -> None:
    """A reader given two of three figures will infer the third."""
    with pytest.raises(ValueError, match="partial one invites"):
        CounterfactualOutcome(
            observation_id="obs-1",
            policy_identity="counterfactual-policy-v1-x",
            state=CounterfactualState.EVALUATED,
            horizon_session=SESSIONS[20],
            evaluated_at=NOW,
            gross_return_bps=Decimal("200"),
            detail="gross only",
        )


# --- the arithmetic -------------------------------------------------------------------------------


def test_the_outcome_is_benchmark_relative_and_net_of_cost() -> None:
    """Both subtractions matter, and each is a way the number would otherwise flatter the gate.

    A raw return credits a refusal for market direction; a frictionless one credits it with a
    fill nobody could get. Together they systematically overstate what the gate gave up.
    """
    outcome = evaluate()

    assert outcome.state is CounterfactualState.EVALUATED
    assert outcome.gross_return_bps == Decimal("2000.00")
    assert outcome.benchmark_return_bps == Decimal("500.00")
    # 2000 gross - 500 benchmark - 10 round-trip cost.
    assert outcome.net_excess_bps == Decimal("1490.00")


def test_raising_the_assumed_cost_lowers_what_the_refusal_appears_to_have_given_up() -> None:
    """The cost is load-bearing, not decorative."""
    cheap = evaluate(policy=policy(round_trip_cost_bps=Decimal("0")))
    dear = evaluate(policy=policy(round_trip_cost_bps=Decimal("300")))

    assert cheap.net_excess_bps is not None and dear.net_excess_bps is not None
    assert cheap.net_excess_bps - dear.net_excess_bps == Decimal("300")


def test_a_short_candidate_profits_from_the_move_a_long_one_loses_on() -> None:
    """Direction flips both the gross and the benchmark leg, not just the first."""
    long_outcome = evaluate(direction=CandidateDirection.LONG)
    short_outcome = evaluate(direction=CandidateDirection.SHORT)

    assert long_outcome.gross_return_bps == Decimal("2000.00")
    assert short_outcome.gross_return_bps == Decimal("-2000.00")
    assert short_outcome.benchmark_return_bps == Decimal("-500.00")
    # Cost is charged in both directions; it is never a credit.
    assert short_outcome.net_excess_bps == Decimal("-1510.00")


def test_the_outcome_references_the_observation_rather_than_amending_it() -> None:
    """Append-only: the refusal keeps saying what was known when it was made.

    Rewriting it after hindsight arrives would destroy the only record of what the system
    actually knew at the moment it decided, which is the thing #57 studies.
    """
    outcome = evaluate()
    row = outcome.as_row()

    assert row["observation_id"] == "obs-1"
    assert row["outcome_id"] == outcome.identity
    assert outcome.identity.startswith("counterfactual-outcome-")
    # Nothing on the outcome can address an order.
    assert not {"quantity", "client_order_id", "side"} & set(row)
