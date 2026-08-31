"""Protective exits that do not depend on the loop that opened the position (#157).

The invariant: once a position exists, safety-critical exit evaluation must not depend on the
code path that decided to enter it.

It was broken, measurably. Every protective exit is produced by `SignalAction.EXIT` inside the
strategy evaluation, which `RunOnceCycle.run` calls *after* halting on reconciliation failure, an
engaged kill switch, or stale data. So a stopped daemon evaluates nothing -- seven open positions
went eleven days unevaluated on the operator's store -- and a running daemon that trips any of
those halts also evaluates nothing, which makes the system least protective exactly when it is
least healthy.

The fault-injection tests are the point of this file. Each one removes a thing the old path
depended on and asserts the decision still happens.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quantbot.domain import Position
from quantbot.risk.protection import (
    ProtectionOutcome,
    ProtectionPolicy,
    ProtectionRule,
    evaluate_position,
    evaluate_positions,
    unprotected,
)

NOW = datetime(2026, 8, 30, 20, 0, tzinfo=UTC)


def position(symbol: str = "XLE", *, entry: str = "100", quantity: str = "1") -> Position:
    return Position(
        symbol=symbol,
        quantity=Decimal(quantity),
        market_price=Decimal(entry),
        market_value=Decimal(entry) * Decimal(quantity),
        average_entry_price=Decimal(entry),
    )


def decide(**kwargs):  # type: ignore[no-untyped-def]
    fields: dict[str, object] = {
        "policy": ProtectionPolicy(),
        "price": Decimal("100"),
        "priced_at": NOW,
        "observed_at": NOW,
        "reconciled": True,
    }
    fields.update(kwargs)
    subject = fields.pop("position", position())
    return evaluate_position(subject, **fields)  # type: ignore[arg-type]


# --- the rules -------------------------------------------------------------------------------


def test_a_position_past_the_hard_stop_requires_an_exit() -> None:
    """The rule the whole thing exists to fire."""
    result = decide(price=Decimal("75"))

    assert result.requires_exit
    assert result.rule is ProtectionRule.HARD_STOP
    assert result.move_bps == Decimal("-2500")
    assert "hard stop" in result.reason


def test_a_position_inside_the_stop_requires_nothing() -> None:
    """A protection that fires on a healthy position is one somebody disables."""
    result = decide(price=Decimal("95"))

    assert result.outcome is ProtectionOutcome.NO_ACTION
    assert result.protected


def test_account_drawdown_is_reported_ahead_of_a_symbol_stop() -> None:
    """It closes everything, so naming one symbol's stop would name the smaller cause."""
    result = decide(price=Decimal("75"), account_drawdown_bps=Decimal("2500"))

    assert result.requires_exit
    assert result.rule is ProtectionRule.ACCOUNT_DRAWDOWN


# --- the states that must not read as safety ---------------------------------------------------


def test_an_unreconciled_position_is_blocked_not_passed() -> None:
    """The 2026-08-19 halt condition, and the one that must not silently mean "fine".

    Exiting on a quantity nobody has confirmed could close a position that is not there or leave
    one that is. Blocking is right; skipping the evaluation is what the old path did.
    """
    result = decide(reconciled=False, price=Decimal("50"))

    assert result.outcome is ProtectionOutcome.BLOCKED_STATE
    assert not result.protected, "a position nobody could evaluate is not a protected one"
    assert "disagree" in result.reason


def test_a_stale_price_is_blocked_rather_than_used_as_current() -> None:
    """Using a week-old price is how a stop fires at a level that no longer exists."""
    result = decide(priced_at=NOW - timedelta(days=9), price=Decimal("75"))

    assert result.outcome is ProtectionOutcome.BLOCKED_STATE
    assert not result.protected
    assert "9 days old" in result.reason


def test_a_missing_price_is_blocked() -> None:
    result = decide(price=None, priced_at=None)

    assert result.outcome is ProtectionOutcome.BLOCKED_STATE
    assert "no price" in result.reason


def test_a_nonsensical_entry_price_is_an_incident_not_a_pass() -> None:
    """Something is wrong with the record rather than with the position.

    Attributable, and specifically not `NO_ACTION` -- a position whose entry cannot be read is
    one whose protection cannot be computed, and reporting that as healthy is the failure this
    module exists for.
    """
    broken = Position(
        symbol="XLE",
        quantity=Decimal("1"),
        market_price=Decimal("100"),
        market_value=Decimal("100"),
        average_entry_price=Decimal("100"),
    )
    object.__setattr__(broken, "__dict__", {**broken.__dict__, "average_entry_price": Decimal("0")})

    result = decide(position=broken)

    assert result.outcome is ProtectionOutcome.INCIDENT
    assert not result.protected


# --- independence: the criteria this issue turns on --------------------------------------------


def test_the_module_imports_nothing_that_could_stop_it() -> None:
    """Independence asserted against the import graph, not against prose.

    My first two versions of this were weak and worth recording. One asserted a phrase appeared
    in the module docstring -- which I wrote, so it proved I had written it. The other compared
    `dir(module)` against a set of names that could never have appeared there.

    This parses the source and checks what it actually imports. If a future edit reaches for a
    strategy runner, a cycle, a model runtime or a research worker to make a protective decision,
    the independence this issue requires is gone and this fails -- which is the only version of
    the claim that a reader should believe.
    """
    import ast

    source = pathlib.Path("src/quantbot/risk/protection.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    forbidden = [
        name
        for name in imported
        if name.startswith(
            (
                "quantbot.strategy",
                "quantbot.operations",
                "quantbot.research",
                "quantbot.brokers",
                "quantbot.execution",
                "quantbot.anomaly",
                "quantbot.forecasting",
            )
        )
    ]

    assert not forbidden, (
        f"protective evaluation must not depend on {forbidden}; a stopped strategy, a dead "
        "model or an absent research worker would take protection down with it"
    )
    assert any(name.startswith("quantbot.domain") for name in imported), (
        "it should still read a Position from the domain, or this test is passing on an empty "
        "import list"
    )


def test_a_decision_is_produced_from_a_position_a_price_and_a_policy_alone() -> None:
    """The functional half of independence.

    No runner, no cycle, no session, no signal -- the three arguments that decide an exit are
    things a supervisor can obtain from broker state and configuration while everything else is
    down.
    """
    assert decide(price=Decimal("70")).requires_exit
    assert decide(price=Decimal("99")).outcome is ProtectionOutcome.NO_ACTION


def test_an_entry_veto_cannot_block_a_protective_exit() -> None:
    """`risk/gates.py` already scopes vetoes to ENTRY only; this asserts protection is separate.

    Nothing in this module takes a veto, a policy version or a gate result, so there is no input
    through which a new-risk refusal could reach an exit decision.
    """
    import inspect

    signature = inspect.signature(evaluate_position)

    assert "veto" not in signature.parameters
    assert "gate" not in signature.parameters
    assert decide(price=Decimal("70")).requires_exit


def test_the_same_inputs_decide_the_same_way_twice() -> None:
    """Duplicate supervisor cycles must not produce different answers.

    Idempotent exits are an execution concern, and this is its precondition: a decision that
    varied between two identical evaluations could not be reconciled against broker state at all.
    """
    first = decide(price=Decimal("70"))
    second = decide(price=Decimal("70"))

    assert (first.outcome, first.rule, first.move_bps) == (
        second.outcome,
        second.rule,
        second.move_bps,
    )


# --- coverage over the whole book --------------------------------------------------------------


def test_every_position_gets_a_decision_including_the_unevaluable_ones() -> None:
    """"Every open position is covered by a measurable protection state" needs the counts to match.

    A position omitted because it had no price would make the covered count look complete while
    the position nobody could evaluate quietly vanished -- which is exactly the shape of the
    original defect.
    """
    book = [position("XLE"), position("XLK"), position("MDY")]

    decisions = evaluate_positions(
        book,
        policy=ProtectionPolicy(),
        prices={"XLE": (Decimal("70"), NOW), "XLK": (Decimal("99"), NOW)},
        observed_at=NOW,
        reconciled=True,
    )

    assert len(decisions) == len(book)
    assert {d.symbol for d in decisions} == {"XLE", "XLK", "MDY"}
    outcomes = {d.symbol: d.outcome for d in decisions}
    assert outcomes["XLE"] is ProtectionOutcome.EXIT_REQUIRED
    assert outcomes["XLK"] is ProtectionOutcome.NO_ACTION
    assert outcomes["MDY"] is ProtectionOutcome.BLOCKED_STATE


def test_unprotected_names_the_positions_nobody_could_evaluate() -> None:
    """The number an operator needs, and the one an empty-list test would not demonstrate."""
    decisions = evaluate_positions(
        [position("XLE"), position("MDY")],
        policy=ProtectionPolicy(),
        prices={"XLE": (Decimal("99"), NOW)},
        observed_at=NOW,
        reconciled=True,
    )

    assert [d.symbol for d in unprotected(decisions)] == ["MDY"]


def test_a_fully_priced_reconciled_book_leaves_nothing_unprotected() -> None:
    decisions = evaluate_positions(
        [position("XLE"), position("XLK")],
        policy=ProtectionPolicy(),
        prices={"XLE": (Decimal("99"), NOW), "XLK": (Decimal("101"), NOW)},
        observed_at=NOW,
        reconciled=True,
    )

    assert unprotected(decisions) == ()


def test_an_unreconciled_book_leaves_everything_unprotected() -> None:
    """The eleven-day state, expressed as the number the console will show.

    Seven positions and no usable state is seven unprotected positions, not zero problems.
    """
    decisions = evaluate_positions(
        [position("XLE"), position("XLK")],
        policy=ProtectionPolicy(),
        prices={"XLE": (Decimal("99"), NOW), "XLK": (Decimal("101"), NOW)},
        observed_at=NOW,
        reconciled=False,
    )

    assert len(unprotected(decisions)) == 2


@pytest.mark.parametrize("days", [0, 1, 4])
def test_a_price_within_the_window_is_usable(days: int) -> None:
    """Four days covers a long weekend plus a holiday, matching STALE_RUN_DAYS deliberately."""
    result = decide(priced_at=NOW - timedelta(days=days), price=Decimal("99"))

    assert result.outcome is ProtectionOutcome.NO_ACTION


def test_a_price_past_the_window_is_not() -> None:
    result = decide(priced_at=NOW - timedelta(days=5), price=Decimal("99"))

    assert result.outcome is ProtectionOutcome.BLOCKED_STATE
