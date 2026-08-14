"""Deterministic next-session and protective-stop fill assumptions."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quantbot.domain import Bar, OrderSide

_BASIS_POINTS = Decimal("10000")


class BacktestLookaheadError(ValueError):
    """Raised when a simulated fill uses a bar not later than its signal."""


class BacktestOrderPurpose(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    STOP = "STOP"
    REBALANCE = "REBALANCE"
    DRAWDOWN_LIQUIDATION = "DRAWDOWN_LIQUIDATION"


class FillModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def normalize_datetime(cls, value: object) -> object:
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)


class ExecutionCosts(FillModel):
    slippage_bps: int = Field(default=0, ge=0, lt=10000)
    commission_per_order: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        allow_inf_nan=False,
    )


class SimulatedOrder(FillModel):
    order_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: OrderSide
    quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    signal_at: datetime
    purpose: BacktestOrderPurpose
    stop_price: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        if value != value.strip() or value != value.upper():
            raise ValueError("symbol must be uppercase without surrounding whitespace")
        return value

    @model_validator(mode="after")
    def validate_stop(self) -> SimulatedOrder:
        if self.purpose is BacktestOrderPurpose.STOP:
            if self.side is not OrderSide.SELL or self.stop_price is None:
                raise ValueError("V1 stop orders must be long-position SELL stops")
        elif self.stop_price is not None:
            raise ValueError("only STOP orders may specify stop_price")
        return self


class SimulatedFill(FillModel):
    order_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: OrderSide
    quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    base_price: Decimal = Field(gt=0, allow_inf_nan=False)
    price: Decimal = Field(gt=0, allow_inf_nan=False)
    occurred_at: datetime
    gross_value: Decimal = Field(gt=0, allow_inf_nan=False)
    commission: Decimal = Field(ge=0, allow_inf_nan=False)
    slippage_cost: Decimal = Field(ge=0, allow_inf_nan=False)
    cash_delta: Decimal = Field(allow_inf_nan=False)
    purpose: BacktestOrderPurpose

    @model_validator(mode="after")
    def validate_accounting(self) -> SimulatedFill:
        if self.gross_value != self.price * self.quantity:
            raise ValueError("fill gross_value must equal price times quantity")
        if self.slippage_cost != abs(self.price - self.base_price) * self.quantity:
            raise ValueError("fill slippage_cost is inconsistent")
        signed_gross = -self.gross_value if self.side is OrderSide.BUY else self.gross_value
        if self.cash_delta != signed_gross - self.commission:
            raise ValueError("fill cash_delta is inconsistent")
        return self


def _validate_fill_bar(order: SimulatedOrder, bar: Bar) -> None:
    if bar.symbol != order.symbol:
        raise ValueError("fill bar symbol must match order symbol")
    if bar.timestamp <= order.signal_at:
        raise BacktestLookaheadError("fill bar must be from a later session than the signal")


def _fill(
    order: SimulatedOrder,
    bar: Bar,
    costs: ExecutionCosts,
    base_price: Decimal,
) -> SimulatedFill:
    slippage_fraction = Decimal(costs.slippage_bps) / _BASIS_POINTS
    if order.side is OrderSide.BUY:
        price = base_price * (Decimal("1") + slippage_fraction)
    else:
        price = base_price * (Decimal("1") - slippage_fraction)
    gross_value = price * order.quantity
    slippage_cost = abs(price - base_price) * order.quantity
    signed_gross = -gross_value if order.side is OrderSide.BUY else gross_value
    cash_delta = signed_gross - costs.commission_per_order
    return SimulatedFill(
        order_id=order.order_id,
        symbol=order.symbol,
        side=order.side,
        quantity=order.quantity,
        base_price=base_price,
        price=price,
        occurred_at=bar.timestamp,
        gross_value=gross_value,
        commission=costs.commission_per_order,
        slippage_cost=slippage_cost,
        cash_delta=cash_delta,
        purpose=order.purpose,
    )


def fill_order_at_next_open(
    order: SimulatedOrder,
    bar: Bar,
    costs: ExecutionCosts,
) -> SimulatedFill:
    """Fill a completed-close decision at the next supplied session open."""
    _validate_fill_bar(order, bar)
    if order.purpose is BacktestOrderPurpose.STOP:
        raise ValueError("protective stops must use fill_stop_order")
    return _fill(order, bar, costs, bar.open)


def fill_stop_order(
    order: SimulatedOrder,
    bar: Bar,
    costs: ExecutionCosts,
) -> SimulatedFill | None:
    """Apply conservative long-stop gap handling using one completed session bar."""
    _validate_fill_bar(order, bar)
    if order.purpose is not BacktestOrderPurpose.STOP or order.stop_price is None:
        raise ValueError("fill_stop_order requires a protective STOP order")
    if bar.open <= order.stop_price:
        base_price = bar.open
    elif bar.low <= order.stop_price:
        base_price = order.stop_price
    else:
        return None
    return _fill(order, bar, costs, base_price)
