"""Tranche scheduling and weighting.

The property that matters most is the first test: a single tranche must reproduce month-end
rebalancing exactly. If that ever breaks, enabling the feature is no longer the only thing that
changes behaviour, and every deployed version silently starts trading on different days.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantbot.strategy.tranches import (
    max_supportable_tranches,
    tranche_schedule,
    tranche_weights,
)


def _months(*counts: tuple[str, int]) -> list[str]:
    out: list[str] = []
    for month, sessions in counts:
        out.extend([month] * sessions)
    return out


def test_a_single_tranche_is_exactly_month_end_rebalancing() -> None:
    """The compatibility guarantee: tranching off must change nothing."""
    months = _months(("2026-01", 20), ("2026-02", 19), ("2026-03", 22))

    schedule = tranche_schedule(months, 1)

    # Final session of each month: indices 19, 38, 60.
    assert sorted(schedule) == [19, 38, 60]
    assert set(schedule.values()) == {0}


def test_five_tranches_spread_through_the_month_and_still_end_on_month_end() -> None:
    months = _months(("2026-01", 21),)

    schedule = tranche_schedule(months, 5)

    assert sorted(schedule) == [3, 7, 12, 16, 20]
    assert schedule[20] == 4, "the last tranche must land on the month's final session"
    assert sorted(schedule.values()) == [0, 1, 2, 3, 4], "each tranche rebalances exactly once"


def test_every_tranche_rebalances_once_per_month_for_any_month_length() -> None:
    """Short months are where an off-by-one would put two tranches on one day."""
    for sessions in range(5, 24):
        months = _months(("2026-01", sessions))

        schedule = tranche_schedule(months, 5)

        assert sorted(schedule.values()) == [0, 1, 2, 3, 4], f"{sessions} sessions"
        assert max(schedule) == sessions - 1, f"{sessions} sessions must end on month end"


def test_a_month_shorter_than_the_tranche_count_does_not_lose_tranches() -> None:
    """Degenerate but real: a holiday-shortened month with fewer sessions than tranches."""
    months = _months(("2026-01", 3))

    schedule = tranche_schedule(months, 5)

    assert schedule, "must still schedule something rather than silently skipping the month"
    assert max(schedule) == 2
    assert all(0 <= index <= 2 for index in schedule)


def test_tranches_must_be_positive() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        tranche_schedule(["2026-01"], 0)


def test_weights_are_fractional_in_proportion_to_tranches_holding_a_name() -> None:
    full = Decimal("0.10")
    holdings = [["SPY", "QQQ"], ["SPY", "QQQ"], ["SPY", "XLK"], ["SPY", "XLK"], ["XLE", "XLK"]]

    weights = tranche_weights(holdings, full)

    assert weights["SPY"] == full * 4 / 5
    assert weights["XLK"] == full * 3 / 5
    assert weights["QQQ"] == full * 2 / 5
    assert weights["XLE"] == full * 1 / 5


def test_unanimous_holdings_reproduce_a_full_position() -> None:
    """Tranching must not shrink exposure when every tranche agrees."""
    full = Decimal("0.10")

    weights = tranche_weights([["SPY"]] * 5, full)

    assert weights == {"SPY": full}


def test_a_single_tranche_gives_full_weight_so_exposure_is_unchanged() -> None:
    full = Decimal("0.10")

    assert tranche_weights([["SPY", "QQQ"]], full) == {"SPY": full, "QQQ": full}


def test_the_broker_minimum_bounds_the_tranche_count_at_small_accounts() -> None:
    """The measured constraint: $100 supports 10 tranches, not the 21 that removes
    all of the dispersion."""
    full = Decimal("0.10")

    assert max_supportable_tranches(Decimal("100"), full) == 10, "$10 position / $1 minimum"
    assert max_supportable_tranches(Decimal("50"), full) == 5
    assert max_supportable_tranches(Decimal("20"), full) == 2


def test_the_constraint_relaxes_as_the_account_grows() -> None:
    """Above roughly $210 even 21 tranches clears the minimum, so this must not stay pinned."""
    full = Decimal("0.10")

    assert max_supportable_tranches(Decimal("210"), full) == 21
    assert max_supportable_tranches(Decimal("10000"), full) == 21, "capped at the ceiling"


def test_a_degenerate_account_still_yields_a_usable_tranche_count() -> None:
    full = Decimal("0.10")

    assert max_supportable_tranches(Decimal("0"), full) == 1
    assert max_supportable_tranches(Decimal("100"), Decimal("0")) == 1
    assert max_supportable_tranches(Decimal("5"), full) == 1, "never returns zero"
