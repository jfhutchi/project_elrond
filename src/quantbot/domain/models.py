"""Frozen and validated trading domain models."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from quantbot.domain.enums import IntentState, OrderSide, OrderType, TimeInForce

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveDecimal = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]


class FrozenModel(BaseModel):
    """Base model enforcing immutable values and normalized UTC timestamps."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def normalize_datetime(cls, value: object) -> object:
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)


class SymbolModel(FrozenModel):
    symbol: str

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        symbol = value.strip()
        if not symbol:
            raise ValueError("symbol must not be empty")
        if symbol != symbol.upper():
            raise ValueError("symbol must be uppercase")
        return symbol


class Bar(SymbolModel):
    timestamp: datetime
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: PositiveDecimal
    adjustment: PositiveDecimal

    @model_validator(mode="after")
    def validate_ohlc(self) -> Self:
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("high must be greater than or equal to open, low, and close")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("low must be less than or equal to open, high, and close")
        return self


class Account(FrozenModel):
    account_id: NonEmptyString
    cash: NonNegativeDecimal
    buying_power: NonNegativeDecimal
    equity: NonNegativeDecimal
    currency: str

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        currency = value.strip()
        if not currency:
            raise ValueError("currency must not be empty")
        if currency != currency.upper():
            raise ValueError("currency must be uppercase")
        return currency


class Position(SymbolModel):
    quantity: FiniteDecimal
    market_price: PositiveDecimal
    market_value: FiniteDecimal
    average_entry_price: PositiveDecimal


class OrderIntent(SymbolModel):
    intent_id: NonEmptyString
    client_order_id: NonEmptyString
    strategy_id: NonEmptyString
    signal_date: date
    side: OrderSide
    order_type: OrderType
    time_in_force: TimeInForce
    quantity: PositiveDecimal
    limit_price: PositiveDecimal | None = None
    stop_price: PositiveDecimal | None = None
    extended_hours: bool = False
    state: IntentState = IntentState.RISK_APPROVED
    created_at: datetime

    @model_validator(mode="after")
    def validate_order_prices(self) -> Self:
        if self.order_type is OrderType.MARKET:
            valid_prices = self.limit_price is None and self.stop_price is None
            requirement = "MARKET orders cannot specify limit_price or stop_price"
        elif self.order_type is OrderType.LIMIT:
            valid_prices = self.limit_price is not None and self.stop_price is None
            requirement = "LIMIT orders require limit_price and cannot specify stop_price"
        elif self.order_type is OrderType.STOP:
            valid_prices = self.limit_price is None and self.stop_price is not None
            requirement = "STOP orders require stop_price and cannot specify limit_price"
        else:
            valid_prices = self.limit_price is not None and self.stop_price is not None
            requirement = "STOP_LIMIT orders require both limit_price and stop_price"

        if not valid_prices:
            raise ValueError(requirement)
        return self


class BrokerOrder(SymbolModel):
    broker_order_id: NonEmptyString
    client_order_id: NonEmptyString
    side: OrderSide
    order_type: OrderType
    time_in_force: TimeInForce
    quantity: PositiveDecimal
    filled_quantity: NonNegativeDecimal
    status: NonEmptyString
    submitted_at: datetime
    filled_average_price: PositiveDecimal | None = None

    @model_validator(mode="after")
    def validate_filled_quantity(self) -> Self:
        if self.filled_quantity > self.quantity:
            raise ValueError("filled_quantity cannot exceed quantity")
        return self


class Fill(SymbolModel):
    fill_id: NonEmptyString
    broker_order_id: NonEmptyString
    side: OrderSide
    quantity: PositiveDecimal
    price: PositiveDecimal
    occurred_at: datetime
    fee: NonNegativeDecimal = Decimal("0")


class MarketClock(FrozenModel):
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


class StrategyIdentity(FrozenModel):
    strategy_id: NonEmptyString
    version: NonEmptyString
    git_commit: NonEmptyString
    configuration_hash: NonEmptyString
    deployment_timestamp: datetime
