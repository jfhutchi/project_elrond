from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from quantbot.backtest import BacktestEngine, BenchmarkVariant
from quantbot.domain import Bar
from quantbot.strategy import load_strategy_config

START = datetime(2025, 1, 2, 21, 0, tzinfo=UTC)


def _bars(future_close: Decimal) -> tuple[Bar, ...]:
    closes = (Decimal("100"), Decimal("101"), Decimal("102"), Decimal("103"), future_close)
    bars: list[Bar] = []
    for index, close in enumerate(closes):
        bars.append(
            Bar(
                symbol="SPY",
                timestamp=START + timedelta(days=index),
                open=close,
                high=close + Decimal("1"),
                low=close - Decimal("1"),
                close=close,
                volume=Decimal("1000"),
                adjustment=Decimal("1"),
            )
        )
    return tuple(bars)


@given(future_close=st.decimals(min_value="10", max_value="1000", places=2))
@settings(max_examples=20, deadline=None)
def test_mutating_future_bar_cannot_change_prior_results(future_close: Decimal) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    config = load_strategy_config(repo_root / "config" / "strategy-v1.yaml").model_copy(
        update={
            "trend_period": 2,
            "slippage_bps": 0,
            "commission_per_order": Decimal("0"),
        }
    )
    engine = BacktestEngine(config, initial_cash=Decimal("1000"))
    baseline = engine.run({"SPY": _bars(Decimal("104"))}, BenchmarkVariant.SPY_SMA200)
    mutated = engine.run({"SPY": _bars(future_close)}, BenchmarkVariant.SPY_SMA200)
    cutoff = START + timedelta(days=3)

    assert tuple(fill for fill in baseline.fills if fill.occurred_at <= cutoff) == tuple(
        fill for fill in mutated.fills if fill.occurred_at <= cutoff
    )
    assert tuple(point for point in baseline.equity_curve if point.timestamp <= cutoff) == tuple(
        point for point in mutated.equity_curve if point.timestamp <= cutoff
    )


@given(future_close=st.decimals(min_value="10", max_value="1000", places=2))
@settings(max_examples=10, deadline=None)
def test_full_strategy_future_mutation_cannot_change_prior_fills(
    future_close: Decimal,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    config = load_strategy_config(repo_root / "config" / "strategy-v1.yaml").model_copy(
        update={
            "universe": ("SPY",),
            "roster_size": 1,
            "momentum_long": 2,
            "momentum_skip": 0,
            "trend_period": 2,
            "entry_period": 2,
            "exit_period": 1,
            "atr_period": 2,
            "slippage_bps": 0,
            "commission_per_order": Decimal("0"),
        }
    )
    dates = (
        datetime(2025, 1, 29, 21, 0, tzinfo=UTC),
        datetime(2025, 1, 30, 21, 0, tzinfo=UTC),
        datetime(2025, 1, 31, 21, 0, tzinfo=UTC),
        datetime(2025, 2, 3, 21, 0, tzinfo=UTC),
        datetime(2025, 2, 4, 21, 0, tzinfo=UTC),
    )

    def full_bars(last_close: Decimal) -> tuple[Bar, ...]:
        closes = (Decimal("100"), Decimal("102"), Decimal("105"), Decimal("107"), last_close)
        return tuple(
            Bar(
                symbol="SPY",
                timestamp=timestamp,
                open=close,
                high=close + Decimal("1"),
                low=max(Decimal("0.01"), close - Decimal("1")),
                close=close,
                volume=Decimal("1000"),
                adjustment=Decimal("1"),
            )
            for timestamp, close in zip(dates, closes, strict=True)
        )

    engine = BacktestEngine(config, initial_cash=Decimal("10000"))
    baseline = engine.run(
        {"SPY": full_bars(Decimal("109"))},
        BenchmarkVariant.FULL_STRATEGY,
    )
    mutated = engine.run(
        {"SPY": full_bars(future_close)},
        BenchmarkVariant.FULL_STRATEGY,
    )
    cutoff = dates[3]

    assert tuple(fill for fill in baseline.fills if fill.occurred_at <= cutoff) == tuple(
        fill for fill in mutated.fills if fill.occurred_at <= cutoff
    )
