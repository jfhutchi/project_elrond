"""Building rosters on tranche schedules, and the blend that averages away the rebalance date.

Step 3 of docs/tranching-spec.md. `build_monthly_roster` previously insisted the evaluation
date be the final XNYS session of its month. Under tranching there are five rebalance dates a
month, one per tranche, so the guard has to become schedule-aware without changing what it
means at a single tranche.
"""

from __future__ import annotations

from decimal import Decimal

from quantbot.strategy import tranche_weights


def test_a_symbol_in_every_tranche_gets_a_full_position() -> None:
    holdings = [["SPY", "QQQ"], ["SPY"], ["SPY"], ["SPY"], ["SPY"]]

    weights = tranche_weights(holdings, Decimal("1"))

    assert weights["SPY"] == Decimal("1")


def test_a_symbol_in_three_of_five_tranches_gets_three_fifths() -> None:
    """The mechanism: membership stops being binary, so the rebalance date stops mattering."""
    holdings = [["XLK"], ["XLK"], ["XLK"], ["XLV"], ["XLV"]]

    weights = tranche_weights(holdings, Decimal("1"))

    assert weights["XLK"] == Decimal("3") / Decimal("5")
    assert weights["XLV"] == Decimal("2") / Decimal("5")


def test_weights_scale_with_the_full_position_size() -> None:
    """A tranche slice is a fraction of the position cap, not a fraction of equity."""
    weights = tranche_weights([["SPY"], ["SPY"], [], [], []], Decimal("0.10"))

    assert weights["SPY"] == Decimal("0.10") * Decimal("2") / Decimal("5")


def test_a_single_tranche_reproduces_binary_membership() -> None:
    """The untranched case: held or not, at full size. Every deployed version means this."""
    weights = tranche_weights([["SPY", "QQQ"]], Decimal("0.10"))

    assert weights == {"QQQ": Decimal("0.10"), "SPY": Decimal("0.10")}


def test_an_unstarted_tranche_must_not_be_passed_as_an_empty_roster() -> None:
    """Holding nothing and having no opinion yet are different, and the difference dilutes.

    During the first month of tranching some tranches have not rebalanced. Representing them as
    empty rosters would divide every weight by the full tranche count and silently under-invest
    the whole account. The runner omits them instead; this pins why that matters.
    """
    started_only = tranche_weights([["SPY"], ["SPY"]], Decimal("1"))
    padded_with_empties = tranche_weights([["SPY"], ["SPY"], [], [], []], Decimal("1"))

    assert started_only["SPY"] == Decimal("1")
    assert padded_with_empties["SPY"] == Decimal("2") / Decimal("5")
    assert started_only["SPY"] != padded_with_empties["SPY"]


def test_a_tranche_holding_a_name_twice_still_counts_once() -> None:
    """Guards against a duplicate in one roster inflating a position beyond its cap."""
    weights = tranche_weights([["SPY", "SPY"], ["SPY"]], Decimal("1"))

    assert weights["SPY"] == Decimal("1")


def test_no_tranches_yields_no_targets() -> None:
    assert tranche_weights([], Decimal("1")) == {}
