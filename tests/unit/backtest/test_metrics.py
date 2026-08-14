from datetime import UTC, datetime, timedelta
from decimal import Decimal

from quantbot.backtest.metrics import (
    EquityObservation,
    TradeOutcome,
    calculate_metrics,
)

START = datetime(2025, 1, 2, 21, 0, tzinfo=UTC)


def test_known_result_performance_and_trade_metrics() -> None:
    equity = (
        EquityObservation(
            timestamp=START,
            equity=Decimal("100"),
            cash=Decimal("100"),
            gross_exposure=Decimal("0"),
            traded_notional=Decimal("100"),
        ),
        EquityObservation(
            timestamp=START + timedelta(days=1),
            equity=Decimal("110"),
            cash=Decimal("55"),
            gross_exposure=Decimal("55"),
            traded_notional=Decimal("50"),
        ),
        EquityObservation(
            timestamp=START + timedelta(days=2),
            equity=Decimal("104.5"),
            cash=Decimal("52.25"),
            gross_exposure=Decimal("52.25"),
            traded_notional=Decimal("0"),
        ),
    )
    trades = (
        TradeOutcome(
            symbol="SPY",
            opened_at=START,
            closed_at=START + timedelta(days=1),
            gross_pnl=Decimal("12"),
            net_pnl=Decimal("10"),
            costs=Decimal("2"),
            notional=Decimal("100"),
        ),
        TradeOutcome(
            symbol="QQQ",
            opened_at=START,
            closed_at=START + timedelta(days=2),
            gross_pnl=Decimal("-4"),
            net_pnl=Decimal("-5"),
            costs=Decimal("1"),
            notional=Decimal("50"),
        ),
    )

    metrics = calculate_metrics(equity, trades, periods_per_year=2)

    assert metrics.total_return == Decimal("0.045")
    assert metrics.cagr_equivalent.quantize(Decimal("0.000001")) == Decimal("0.045000")
    assert metrics.sharpe is not None
    assert metrics.sortino is not None
    assert metrics.sharpe.quantize(Decimal("0.000001")) == Decimal("0.471405")
    assert metrics.sortino.quantize(Decimal("0.000001")) == Decimal("1.000000")
    assert metrics.maximum_drawdown == Decimal("0.05")
    assert metrics.profit_factor == Decimal("2")
    assert metrics.expectancy == Decimal("2.5")
    assert metrics.win_rate == Decimal("0.5")
    assert metrics.winners == 1
    assert metrics.losers == 1
    assert metrics.exposure_fraction.quantize(Decimal("0.000001")) == Decimal("0.333333")
    assert metrics.turnover.quantize(Decimal("0.000001")) == Decimal("1.430843")
    assert metrics.total_costs == Decimal("3")


def test_empty_trade_set_has_explicit_undefined_ratios() -> None:
    equity = (
        EquityObservation(
            timestamp=START,
            equity=Decimal("100"),
            cash=Decimal("100"),
            gross_exposure=Decimal("0"),
            traded_notional=Decimal("0"),
        ),
    )

    metrics = calculate_metrics(equity, ())

    assert metrics.total_return == Decimal("0")
    assert metrics.sharpe is None
    assert metrics.sortino is None
    assert metrics.profit_factor is None
    assert metrics.expectancy is None
    assert metrics.win_rate is None


def test_initial_equity_override_captures_first_session_drawdown() -> None:
    equity = (
        EquityObservation(
            timestamp=START,
            equity=Decimal("90"),
            cash=Decimal("0"),
            gross_exposure=Decimal("90"),
            traded_notional=Decimal("100"),
        ),
        EquityObservation(
            timestamp=START + timedelta(days=1),
            equity=Decimal("95"),
            cash=Decimal("0"),
            gross_exposure=Decimal("95"),
            traded_notional=Decimal("0"),
        ),
    )

    metrics = calculate_metrics(
        equity,
        (),
        periods_per_year=2,
        initial_equity_override=Decimal("100"),
    )

    assert metrics.total_return == Decimal("-0.05")
    assert metrics.maximum_drawdown == Decimal("0.10")
