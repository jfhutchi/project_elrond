"""Frozen value objects shared by deterministic risk policies."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quantbot.domain import OrderSide, OrderType, Position, TimeInForce

FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
PositiveDecimal = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


class RiskModel(BaseModel):
    """Immutable base model for risk calculations and audit results."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def normalize_datetime(cls, value: object) -> object:
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)


class DrawdownState(RiskModel):
    current_equity: NonNegativeDecimal
    high_water_equity: PositiveDecimal
    drawdown_fraction: Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
    new_risk_multiplier: Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
    entry_halted: bool
    liquidation_required: bool


class PositionRiskInput(RiskModel):
    position: Position
    active_stop: PositiveDecimal | None


class RiskReservation(RiskModel):
    reservation_id: str
    symbol: str
    position_value: PositiveDecimal
    gross_exposure: PositiveDecimal
    open_risk: PositiveDecimal
    reserves_position_slot: bool

    @field_validator("reservation_id")
    @classmethod
    def validate_reservation_id(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("reservation_id must be nonempty without surrounding whitespace")
        return value

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        if not value or value != value.strip() or value != value.upper():
            raise ValueError("symbol must be uppercase")
        return value

    @model_validator(mode="after")
    def validate_exposure(self) -> RiskReservation:
        if self.gross_exposure < self.position_value:
            raise ValueError("gross_exposure cannot be less than position_value")
        return self


class PortfolioRiskState(RiskModel):
    account_snapshot_id: str
    target_symbol: str
    equity: FiniteDecimal
    buying_power: FiniteDecimal
    committed_open_risk: FiniteDecimal
    committed_gross_exposure: FiniteDecimal
    committed_symbol_market_value: FiniteDecimal
    committed_position_count: int
    is_new_position: bool
    open_risk_available: bool
    reasons: tuple[str, ...]

    @field_validator("account_snapshot_id")
    @classmethod
    def validate_snapshot_id(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("account_snapshot_id must be nonempty without surrounding whitespace")
        return value

    @field_validator("target_symbol")
    @classmethod
    def validate_target_symbol(cls, value: str) -> str:
        if not value or value != value.strip() or value != value.upper():
            raise ValueError("target_symbol must be uppercase")
        return value


class SizingCapName(StrEnum):
    PER_TRADE_RISK = "PER_TRADE_RISK"
    PORTFOLIO_OPEN_RISK = "PORTFOLIO_OPEN_RISK"
    POSITION_VALUE = "POSITION_VALUE"
    GROSS_EXPOSURE = "GROSS_EXPOSURE"
    BUYING_POWER = "BUYING_POWER"
    VOLATILITY_TARGET = "VOLATILITY_TARGET"


class OrderPurpose(StrEnum):
    ENTRY = "ENTRY"
    STRATEGY_EXIT = "STRATEGY_EXIT"
    DRAWDOWN_LIQUIDATION = "DRAWDOWN_LIQUIDATION"


class SizingCaps(RiskModel):
    per_trade_risk: NonNegativeDecimal
    portfolio_open_risk: NonNegativeDecimal
    position_value: NonNegativeDecimal
    gross_exposure: NonNegativeDecimal
    buying_power: NonNegativeDecimal
    #: Absent when volatility targeting is disabled, which is every version through 1.2.0.
    volatility_target: NonNegativeDecimal | None = None


class EntrySizingRequest(RiskModel):
    symbol: str
    reference_price: FiniteDecimal
    stop_distance: FiniteDecimal
    portfolio: PortfolioRiskState
    drawdown: DrawdownState
    #: Annualised realised volatility of the symbol, as a fraction. Required only when the
    #: config enables volatility targeting; sizing fails closed if it is then missing, because
    #: silently skipping the cap would size as though the feature were off.
    realized_volatility: FiniteDecimal | None = None

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        if not value or value != value.strip() or value != value.upper():
            raise ValueError("symbol must be uppercase")
        return value

    @model_validator(mode="after")
    def validate_portfolio_target(self) -> EntrySizingRequest:
        if self.symbol != self.portfolio.target_symbol:
            raise ValueError("symbol must match portfolio target symbol")
        return self


class RiskApproval(RiskModel):
    approved: Literal[True] = True
    symbol: str
    quantity: PositiveDecimal
    stop_distance: PositiveDecimal
    drawdown_fraction: Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
    new_risk_multiplier: Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
    effective_risk_fraction: NonNegativeDecimal
    risk_budget: NonNegativeDecimal
    caps: SizingCaps
    limiting_caps: tuple[SizingCapName, ...]
    projected_position_value: NonNegativeDecimal
    projected_gross_exposure: NonNegativeDecimal
    projected_open_risk: NonNegativeDecimal
    fractional_shares: bool = False

    @model_validator(mode="after")
    def validate_whole_share_quantity(self) -> RiskApproval:
        if not self.fractional_shares and self.quantity != self.quantity.to_integral_value():
            raise ValueError("approved quantity must be a whole number of shares")
        return self


class RiskRejection(RiskModel):
    approved: Literal[False] = False
    symbol: str
    reasons: tuple[str, ...]
    drawdown_fraction: Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
    new_risk_multiplier: Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
    caps: SizingCaps | None = None


class OrderGateDecision(RiskModel):
    purpose: OrderPurpose
    allowed: bool
    reasons: tuple[str, ...]
    broker_label: str
    environment_label: str


class LiquidationInstruction(RiskModel):
    symbol: str
    side: OrderSide
    quantity: PositiveDecimal
    eligible_at: datetime
    order_type: Literal[OrderType.MARKET] = OrderType.MARKET
    time_in_force: Literal[TimeInForce.DAY] = TimeInForce.DAY
    extended_hours: Literal[False] = False
    reason: Literal["MAX_DRAWDOWN_LIQUIDATION"] = "MAX_DRAWDOWN_LIQUIDATION"

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        if not value or value != value.strip() or value != value.upper():
            raise ValueError("symbol must be uppercase")
        return value


class LiquidationPlan(RiskModel):
    drawdown_fraction: Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
    liquidation_required: bool
    cancel_open_entry_orders: bool
    instructions: tuple[LiquidationInstruction, ...]

    @model_validator(mode="after")
    def validate_consistency(self) -> LiquidationPlan:
        if self.cancel_open_entry_orders != self.liquidation_required:
            raise ValueError("entry-order cancellation must match liquidation requirement")
        if not self.liquidation_required and self.instructions:
            raise ValueError("non-liquidating plans cannot contain instructions")
        return self
