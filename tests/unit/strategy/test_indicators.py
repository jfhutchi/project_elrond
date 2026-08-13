from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.domain import Bar
from quantbot.strategy.indicators import (
    IndicatorInputError,
    donchian_entry_level,
    donchian_exit_level,
    highest_high_since,
    momentum_12_1,
    sma,
    true_ranges,
    wilder_atr,
)

FIXTURE = Path(__file__).parents[2] / "fixtures" / "strategy_bars.csv"
START = datetime(2026, 1, 1, 21, tzinfo=UTC)


def make_bar(
    offset: int,
    close: str,
    *,
    symbol: str = "SPY",
    high: str | None = None,
    low: str | None = None,
) -> Bar:
    close_value = Decimal(close)
    return Bar(
        symbol=symbol,
        timestamp=START + timedelta(days=offset),
        open=close_value,
        high=Decimal(high) if high is not None else close_value + Decimal("1"),
        low=Decimal(low) if low is not None else close_value - Decimal("1"),
        close=close_value,
        volume=100,
        adjustment=1,
    )


@pytest.fixture
def fixture_bars() -> list[Bar]:
    with FIXTURE.open(newline="", encoding="ascii") as source:
        return [Bar.model_validate(row) for row in csv.DictReader(source)]


def test_sma_uses_last_period_including_current_and_returns_decimal(
    fixture_bars: list[Bar],
) -> None:
    assert sma(fixture_bars, 3) == Decimal("13")
    assert isinstance(sma(fixture_bars, 3), Decimal)
    assert sma(fixture_bars[:2], 3) is None
    assert sma(fixture_bars, 1) == Decimal("15")


def test_momentum_uses_skipped_close_and_requires_long_plus_one_bars() -> None:
    bars = [make_bar(index, close) for index, close in enumerate(("10", "12", "13", "11"))]

    assert momentum_12_1(bars, long_period=3, skip=1) == Decimal("0.3")
    assert isinstance(momentum_12_1(bars, long_period=3, skip=1), Decimal)
    assert momentum_12_1(bars[:-1], long_period=3, skip=1) is None


def test_donchian_levels_exclude_current_bar_and_handle_equality() -> None:
    bars = [
        make_bar(0, "10", high="11", low="8"),
        make_bar(1, "12", high="13", low="9"),
        make_bar(2, "13", high="14", low="11"),
        make_bar(3, "11", high="50", low="1"),
    ]

    assert donchian_entry_level(bars, 3) == Decimal("14")
    assert donchian_exit_level(bars, 3) == Decimal("8")
    assert donchian_entry_level(bars[:3], 3) is None
    assert donchian_exit_level(bars[:3], 3) is None


def test_true_ranges_and_wilder_atr_are_exact(fixture_bars: list[Bar]) -> None:
    assert true_ranges(fixture_bars) == tuple(map(Decimal, ("3", "4", "3", "5", "6")))
    assert wilder_atr(fixture_bars[:2], 3) is None
    assert wilder_atr(fixture_bars[:3], 3) == Decimal(10) / Decimal(3)
    assert wilder_atr(fixture_bars, 3) == Decimal(124) / Decimal(27)
    assert isinstance(wilder_atr(fixture_bars, 3), Decimal)


def test_highest_high_since_is_inclusive_and_rejects_naive_timestamp(
    fixture_bars: list[Bar],
) -> None:
    assert highest_high_since(fixture_bars, START + timedelta(days=2)) == Decimal("16")
    assert highest_high_since(fixture_bars, START + timedelta(days=5)) is None

    with pytest.raises(ValueError, match="timezone-aware"):
        highest_high_since(fixture_bars, datetime(2026, 1, 1))


@pytest.mark.parametrize(
    "bars",
    [
        [make_bar(0, "10"), make_bar(0, "11")],
        [make_bar(1, "10"), make_bar(0, "11")],
        [make_bar(0, "10"), make_bar(1, "11", symbol="QQQ")],
    ],
    ids=["duplicate", "nonmonotonic", "mixed-symbol"],
)
def test_indicators_reject_invalid_ordered_series(bars: list[Bar]) -> None:
    functions = (
        lambda: sma(bars, 1),
        lambda: momentum_12_1(bars, long_period=1, skip=0),
        lambda: donchian_entry_level(bars, 1),
        lambda: donchian_exit_level(bars, 1),
        lambda: true_ranges(bars),
        lambda: wilder_atr(bars, 1),
        lambda: highest_high_since(bars, START),
    )

    for invoke in functions:
        with pytest.raises(IndicatorInputError):
            invoke()


@pytest.mark.parametrize("period", [0, -1])
def test_indicators_reject_nonpositive_periods(period: int, fixture_bars: list[Bar]) -> None:
    with pytest.raises(ValueError, match="positive"):
        sma(fixture_bars, period)
    with pytest.raises(ValueError, match="positive"):
        donchian_entry_level(fixture_bars, period)
    with pytest.raises(ValueError, match="positive"):
        donchian_exit_level(fixture_bars, period)
    with pytest.raises(ValueError, match="positive"):
        wilder_atr(fixture_bars, period)


def test_momentum_rejects_nonsensical_period_relationship(fixture_bars: list[Bar]) -> None:
    with pytest.raises(ValueError, match="long_period"):
        momentum_12_1(fixture_bars, long_period=3, skip=3)
