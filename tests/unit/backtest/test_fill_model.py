from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quantbot.backtest.fill_model import (
    BacktestLookaheadError,
    BacktestOrderPurpose,
    ExecutionCosts,
    SimulatedOrder,
    fill_order_at_next_open,
    fill_stop_order,
)
from quantbot.domain import Bar, OrderSide

SIGNAL_AT = datetime(2026, 1, 2, 21, 0, tzinfo=UTC)


def _bar(
    *,
    timestamp: datetime | None = None,
    open_price: str = "100",
    high: str = "105",
    low: str = "95",
    close: str = "102",
    adjustment: str = "1",
) -> Bar:
    return Bar(
        symbol="SPY",
        timestamp=timestamp or SIGNAL_AT + timedelta(days=1),
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1000"),
        adjustment=Decimal(adjustment),
    )


def _order(*, side: OrderSide = OrderSide.BUY, stop_price: str | None = None) -> SimulatedOrder:
    return SimulatedOrder(
        order_id="order-1",
        symbol="SPY",
        side=side,
        quantity=Decimal("10"),
        signal_at=SIGNAL_AT,
        purpose=(
            BacktestOrderPurpose.STOP if stop_price is not None else BacktestOrderPurpose.ENTRY
        ),
        stop_price=Decimal(stop_price) if stop_price is not None else None,
    )


def test_market_order_fills_only_at_next_session_open_with_adverse_costs() -> None:
    costs = ExecutionCosts(slippage_bps=5, commission_per_order=Decimal("1"))

    fill = fill_order_at_next_open(_order(), _bar(), costs)

    assert fill.base_price == Decimal("100")
    assert fill.price == Decimal("100.05")
    assert fill.slippage_cost == Decimal("0.50")
    assert fill.commission == Decimal("1")
    assert fill.cash_delta == Decimal("-1001.50")


def test_same_session_fill_is_rejected_as_lookahead() -> None:
    with pytest.raises(BacktestLookaheadError, match="later session"):
        fill_order_at_next_open(_order(), _bar(timestamp=SIGNAL_AT), ExecutionCosts())


def test_long_stop_gaps_to_open_and_never_assumes_the_stop_price() -> None:
    stop = _order(side=OrderSide.SELL, stop_price="98")
    bar = _bar(open_price="90", high="96", low="85", close="92")

    fill = fill_stop_order(stop, bar, ExecutionCosts(slippage_bps=5))

    assert fill is not None
    assert fill.base_price == Decimal("90")
    assert fill.price == Decimal("89.955")


def test_intraday_stop_fills_at_stop_and_non_trigger_returns_none() -> None:
    stop = _order(side=OrderSide.SELL, stop_price="98")

    triggered = fill_stop_order(stop, _bar(open_price="100", low="97"), ExecutionCosts())
    untouched = fill_stop_order(
        stop,
        _bar(open_price="100", low="99", close="101"),
        ExecutionCosts(),
    )

    assert triggered is not None
    assert triggered.base_price == Decimal("98")
    assert untouched is None


def test_fill_uses_provider_adjusted_ohlc_without_reapplying_adjustment() -> None:
    adjusted = _bar(
        open_price="50",
        high="52",
        low="49",
        close="51",
        adjustment="2",
    )

    fill = fill_order_at_next_open(_order(), adjusted, ExecutionCosts())

    assert fill.base_price == Decimal("50")
    assert fill.gross_value == Decimal("500")
