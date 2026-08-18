"""Unfilled orders must consume the risk budget they already committed.

The defect this pins cost real buying power: a cycle re-run before its orders filled saw
no positions and an empty reservation list, so it sized fresh entries against a budget it
had already spent, deployed a further 20% of equity, and then took an Alpaca 403 mid-
submission — leaving the account unable to act on the next signal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from quantbot.domain import Bar, BrokerOrder, OrderSide, OrderType, TimeInForce
from quantbot.operations.runners import AdaptiveMomentumRunner

EQUITY = Decimal("100")
AT = datetime(2026, 8, 17, 20, 0, tzinfo=UTC)


def _bar(symbol: str, close: str) -> Bar:
    return Bar(
        symbol=symbol, timestamp=AT, open=Decimal(close), high=Decimal(close),
        low=Decimal(close), close=Decimal(close), volume=Decimal("1000"),
        adjustment=Decimal("1"),
    )


def _order(
    symbol: str, qty: str, *, side: OrderSide = OrderSide.BUY, filled: str = "0"
) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=f"b-{symbol}", client_order_id=f"qb-{symbol}", symbol=symbol,
        side=side, order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY,
        quantity=Decimal(qty), filled_quantity=Decimal(filled), status="accepted",
        submitted_at=AT,
    )


def _reserve(orders: list[BrokerOrder]) -> list:
    runner = SimpleNamespace(_config=SimpleNamespace(risk_per_trade_bps=50))
    sliced = {"XLK": [_bar("XLK", "10")], "XLV": [_bar("XLV", "20")]}
    return AdaptiveMomentumRunner._pending_reservations(runner, orders, sliced, EQUITY)


def test_an_unfilled_buy_reserves_its_value_and_its_slot() -> None:
    reserved = _reserve([_order("XLK", "0.5")])

    assert len(reserved) == 1
    assert reserved[0].position_value == Decimal("5.0"), "0.5 shares at 10 is 5 of capital"
    assert reserved[0].reserves_position_slot is True
    assert reserved[0].open_risk == Decimal("0.50"), "0.5% of 100 equity"


def test_every_pending_buy_reserves_so_a_rerun_cannot_double_deploy() -> None:
    """The exact failure: four pending orders must all still bind the next run."""
    reserved = _reserve([_order("XLK", "0.5"), _order("XLV", "0.5")])

    assert len(reserved) == 2
    assert sum(r.position_value for r in reserved) == Decimal("15.0")


def test_a_partial_fill_reserves_only_the_unfilled_remainder() -> None:
    reserved = _reserve([_order("XLK", "1.0", filled="0.4")])

    assert reserved[0].position_value == Decimal("6.0"), "only 0.6 shares are still pending"


def test_a_fully_filled_order_reserves_nothing_because_it_is_now_a_position() -> None:
    assert _reserve([_order("XLK", "1.0", filled="1.0")]) == []


def test_a_pending_sell_reserves_nothing_because_it_frees_capital() -> None:
    assert _reserve([_order("XLK", "1.0", side=OrderSide.SELL)]) == []


def test_an_order_with_no_priced_bar_is_skipped_rather_than_guessed() -> None:
    assert _reserve([_order("NOPE", "1.0")]) == []
