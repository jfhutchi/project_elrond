from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from quantbot.brokers import TradeUpdate, TradeUpdateEvent
from quantbot.domain import BrokerOrder, Fill, OrderSide, OrderType, TimeInForce
from quantbot.execution import (
    AccountingPosition,
    AccountingReconciliationRequired,
    PortfolioAccountingState,
    apply_trade_update,
)

NOW = datetime(2026, 8, 31, 20, tzinfo=UTC)


def broker_order(
    *,
    side: OrderSide = OrderSide.BUY,
    status: str = "partially_filled",
    filled_quantity: str = "4",
) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id="broker-order-1",
        client_order_id="qb-client-1",
        symbol="SPY",
        side=side,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal("10"),
        filled_quantity=Decimal(filled_quantity),
        status=status,
        submitted_at=NOW,
        filled_average_price=Decimal("100"),
    )


def update(
    event: TradeUpdateEvent,
    *,
    fill_id: str = "execution-1",
    side: OrderSide = OrderSide.BUY,
    quantity: str = "4",
    price: str = "100",
    fee: str = "0",
    position_quantity: str = "4",
) -> TradeUpdate:
    has_fill = event in {TradeUpdateEvent.FILL, TradeUpdateEvent.PARTIAL_FILL}
    fill = (
        Fill(
            fill_id=fill_id,
            broker_order_id="broker-order-1",
            symbol="SPY",
            side=side,
            quantity=Decimal(quantity),
            price=Decimal(price),
            occurred_at=NOW,
            fee=Decimal(fee),
        )
        if has_fill
        else None
    )
    return TradeUpdate(
        event=event,
        event_id=fill_id if has_fill else f"event-{event.value}",
        order=broker_order(side=side),
        occurred_at=NOW,
        fill=fill,
        position_quantity=Decimal(position_quantity) if has_fill else None,
        connection_generation=0,
    )


def empty_state() -> PortfolioAccountingState:
    return PortfolioAccountingState(
        cash=Decimal("10000"),
        realized_pnl=Decimal("0"),
        positions=(),
        applied_executions=(),
    )


def test_acknowledgement_never_creates_a_position_or_changes_cash() -> None:
    state = empty_state()

    result = apply_trade_update(state, update(TradeUpdateEvent.ACCEPTED))

    assert result is state


def test_partial_fills_update_only_confirmed_quantity_cash_and_average_cost() -> None:
    first = apply_trade_update(empty_state(), update(TradeUpdateEvent.PARTIAL_FILL))
    second = apply_trade_update(
        first,
        update(
            TradeUpdateEvent.FILL,
            fill_id="execution-2",
            quantity="6",
            price="102",
            position_quantity="10",
        ),
    )

    assert first.cash == Decimal("9600")
    assert first.positions == (
        AccountingPosition(symbol="SPY", quantity=Decimal("4"), average_cost=Decimal("100")),
    )
    assert second.cash == Decimal("8988")
    assert second.positions == (
        AccountingPosition(
            symbol="SPY",
            quantity=Decimal("10"),
            average_cost=Decimal("101.2"),
        ),
    )
    assert second.realized_pnl == Decimal("0")


def test_exact_duplicate_fill_is_idempotent_and_conflict_is_rejected() -> None:
    event = update(TradeUpdateEvent.PARTIAL_FILL)
    once = apply_trade_update(empty_state(), event)

    assert apply_trade_update(once, event) is once
    with pytest.raises(AccountingReconciliationRequired, match="conflicting duplicate"):
        apply_trade_update(
            once,
            update(TradeUpdateEvent.PARTIAL_FILL, price="101"),
        )


def test_fees_and_adverse_slippage_are_accounted_separately() -> None:
    result = apply_trade_update(
        empty_state(),
        update(
            TradeUpdateEvent.PARTIAL_FILL,
            quantity="2",
            price="101",
            fee="1",
            position_quantity="2",
        ),
        reference_price=Decimal("100"),
    )

    assert result.cash == Decimal("9797")
    assert result.realized_pnl == Decimal("-1")
    assert result.fees_paid == Decimal("1")
    assert result.slippage_cost == Decimal("2")
    assert result.positions == (
        AccountingPosition(symbol="SPY", quantity=Decimal("2"), average_cost=Decimal("101")),
    )


def test_duplicate_fill_with_a_different_reference_price_is_rejected() -> None:
    event = update(TradeUpdateEvent.PARTIAL_FILL)
    once = apply_trade_update(empty_state(), event, reference_price=Decimal("100"))

    with pytest.raises(AccountingReconciliationRequired, match="conflicting duplicate"):
        apply_trade_update(once, event, reference_price=Decimal("99"))


def test_sell_reduces_long_and_records_realized_pnl() -> None:
    starting = PortfolioAccountingState(
        cash=Decimal("0"),
        realized_pnl=Decimal("0"),
        positions=(
            AccountingPosition(
                symbol="SPY",
                quantity=Decimal("10"),
                average_cost=Decimal("101.2"),
            ),
        ),
        applied_executions=(),
    )

    result = apply_trade_update(
        starting,
        update(
            TradeUpdateEvent.PARTIAL_FILL,
            side=OrderSide.SELL,
            quantity="4",
            price="110",
            position_quantity="6",
        ),
    )

    assert result.cash == Decimal("440")
    assert result.realized_pnl == Decimal("35.2")
    assert result.positions == (
        AccountingPosition(
            symbol="SPY",
            quantity=Decimal("6"),
            average_cost=Decimal("101.2"),
        ),
    )


def test_crossing_through_zero_opens_new_side_at_execution_price() -> None:
    starting = PortfolioAccountingState(
        cash=Decimal("0"),
        realized_pnl=Decimal("0"),
        positions=(
            AccountingPosition(symbol="SPY", quantity=Decimal("2"), average_cost=Decimal("90")),
        ),
        applied_executions=(),
    )

    result = apply_trade_update(
        starting,
        update(
            TradeUpdateEvent.FILL,
            side=OrderSide.SELL,
            quantity="5",
            price="100",
            position_quantity="-3",
        ),
    )

    assert result.cash == Decimal("500")
    assert result.realized_pnl == Decimal("20")
    assert result.positions == (
        AccountingPosition(symbol="SPY", quantity=Decimal("-3"), average_cost=Decimal("100")),
    )


def test_broker_position_quantity_mismatch_requires_reconciliation() -> None:
    with pytest.raises(AccountingReconciliationRequired, match="position quantity"):
        apply_trade_update(
            empty_state(),
            update(TradeUpdateEvent.PARTIAL_FILL, position_quantity="5"),
        )


@pytest.mark.parametrize(
    "event",
    [TradeUpdateEvent.TRADE_BUST, TradeUpdateEvent.TRADE_CORRECT],
)
def test_trade_corrections_require_authoritative_reconciliation(event: TradeUpdateEvent) -> None:
    correction = TradeUpdate(
        event=event,
        event_id=f"event-{event.value}",
        order=broker_order(),
        occurred_at=NOW,
        fill=None,
        position_quantity=None,
        connection_generation=0,
    )
    with pytest.raises(AccountingReconciliationRequired, match="correction"):
        apply_trade_update(empty_state(), correction)
