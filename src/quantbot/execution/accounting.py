"""Pure portfolio accounting from broker-confirmed execution events."""

from __future__ import annotations

from decimal import Decimal

from pydantic import field_validator, model_validator

from quantbot.brokers import TradeUpdate, TradeUpdateEvent
from quantbot.brokers.base import BrokerModel
from quantbot.domain import Fill, OrderSide


class AccountingReconciliationRequired(RuntimeError):
    """Raised when local fill arithmetic disagrees with authoritative broker data."""


class AccountingPosition(BrokerModel):
    symbol: str
    quantity: Decimal
    average_cost: Decimal

    @model_validator(mode="after")
    def validate_position(self) -> AccountingPosition:
        if (
            not self.symbol
            or self.symbol != self.symbol.strip()
            or self.symbol != self.symbol.upper()
        ):
            raise ValueError("accounting position symbol must be uppercase without whitespace")
        if not self.quantity.is_finite() or self.quantity == 0:
            raise ValueError("accounting position quantity must be finite and nonzero")
        if not self.average_cost.is_finite() or self.average_cost <= 0:
            raise ValueError("accounting position average_cost must be positive and finite")
        return self


class AppliedExecution(BrokerModel):
    fill: Fill
    position_quantity: Decimal
    reference_price: Decimal | None = None

    @model_validator(mode="after")
    def validate_execution(self) -> AppliedExecution:
        if not self.position_quantity.is_finite():
            raise ValueError("execution position_quantity must be finite")
        if self.reference_price is not None and (
            not self.reference_price.is_finite() or self.reference_price <= 0
        ):
            raise ValueError("execution reference_price must be positive and finite")
        return self


class PortfolioAccountingState(BrokerModel):
    cash: Decimal
    realized_pnl: Decimal
    positions: tuple[AccountingPosition, ...] = ()
    applied_executions: tuple[AppliedExecution, ...] = ()
    fees_paid: Decimal = Decimal("0")
    slippage_cost: Decimal = Decimal("0")

    @field_validator("cash", "realized_pnl")
    @classmethod
    def validate_finite_money(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("accounting money values must be finite")
        return value

    @field_validator("fees_paid", "slippage_cost")
    @classmethod
    def validate_nonnegative_cost(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("accounting cost values must be nonnegative and finite")
        return value

    @field_validator("positions")
    @classmethod
    def normalize_positions(
        cls, value: tuple[AccountingPosition, ...]
    ) -> tuple[AccountingPosition, ...]:
        symbols = [position.symbol for position in value]
        if len(symbols) != len(set(symbols)):
            raise ValueError("accounting positions must have unique symbols")
        return tuple(sorted(value, key=lambda position: position.symbol))

    @field_validator("applied_executions")
    @classmethod
    def validate_execution_identity(
        cls, value: tuple[AppliedExecution, ...]
    ) -> tuple[AppliedExecution, ...]:
        fill_ids = [execution.fill.fill_id for execution in value]
        if len(fill_ids) != len(set(fill_ids)):
            raise ValueError("applied execution fill IDs must be unique")
        return value


def _average_after_fill(
    current_quantity: Decimal,
    current_average: Decimal | None,
    signed_fill_quantity: Decimal,
    fill_price: Decimal,
) -> Decimal | None:
    new_quantity = current_quantity + signed_fill_quantity
    if new_quantity == 0:
        return None
    if current_quantity == 0:
        return fill_price
    if current_quantity * signed_fill_quantity > 0:
        assert current_average is not None
        total_cost = (
            abs(current_quantity) * current_average + abs(signed_fill_quantity) * fill_price
        )
        return total_cost / abs(new_quantity)
    if current_quantity * new_quantity > 0:
        return current_average
    return fill_price


def _realized_fill_pnl(
    current_quantity: Decimal,
    current_average: Decimal | None,
    signed_fill_quantity: Decimal,
    fill_price: Decimal,
) -> Decimal:
    if current_quantity == 0 or current_quantity * signed_fill_quantity >= 0:
        return Decimal("0")
    assert current_average is not None
    closed_quantity = min(abs(current_quantity), abs(signed_fill_quantity))
    if current_quantity > 0:
        return (fill_price - current_average) * closed_quantity
    return (current_average - fill_price) * closed_quantity


def _adverse_slippage(fill: Fill, reference_price: Decimal | None) -> Decimal:
    if reference_price is None:
        return Decimal("0")
    if fill.side is OrderSide.BUY:
        per_share = max(fill.price - reference_price, Decimal("0"))
    else:
        per_share = max(reference_price - fill.price, Decimal("0"))
    return per_share * fill.quantity


def apply_trade_update(
    state: PortfolioAccountingState,
    update: TradeUpdate,
    *,
    reference_price: Decimal | None = None,
) -> PortfolioAccountingState:
    """Apply only broker-confirmed execution quantity, exactly once."""
    if update.event in {TradeUpdateEvent.TRADE_BUST, TradeUpdateEvent.TRADE_CORRECT}:
        raise AccountingReconciliationRequired(
            "trade correction requires authoritative broker reconciliation"
        )
    if update.event not in {TradeUpdateEvent.FILL, TradeUpdateEvent.PARTIAL_FILL}:
        return state

    fill = update.fill
    broker_position_quantity = update.position_quantity
    assert fill is not None and broker_position_quantity is not None
    candidate = AppliedExecution(
        fill=fill,
        position_quantity=broker_position_quantity,
        reference_price=reference_price,
    )
    existing = next(
        (
            execution
            for execution in state.applied_executions
            if execution.fill.fill_id == fill.fill_id
        ),
        None,
    )
    if existing is not None:
        if existing == candidate:
            return state
        raise AccountingReconciliationRequired(f"conflicting duplicate execution: {fill.fill_id}")

    positions = {position.symbol: position for position in state.positions}
    current = positions.get(fill.symbol)
    current_quantity = current.quantity if current is not None else Decimal("0")
    current_average = current.average_cost if current is not None else None
    signed_quantity = fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
    new_quantity = current_quantity + signed_quantity
    if new_quantity != broker_position_quantity:
        raise AccountingReconciliationRequired(
            "computed position quantity disagrees with authoritative broker position quantity"
        )

    new_average = _average_after_fill(
        current_quantity,
        current_average,
        signed_quantity,
        fill.price,
    )
    realized = _realized_fill_pnl(
        current_quantity,
        current_average,
        signed_quantity,
        fill.price,
    )
    if new_quantity == 0:
        positions.pop(fill.symbol, None)
    else:
        assert new_average is not None
        positions[fill.symbol] = AccountingPosition(
            symbol=fill.symbol,
            quantity=new_quantity,
            average_cost=new_average,
        )

    return PortfolioAccountingState(
        cash=state.cash - signed_quantity * fill.price - fill.fee,
        realized_pnl=state.realized_pnl + realized - fill.fee,
        positions=tuple(positions.values()),
        applied_executions=(*state.applied_executions, candidate),
        fees_paid=state.fees_paid + fill.fee,
        slippage_cost=state.slippage_cost + _adverse_slippage(fill, reference_price),
    )
