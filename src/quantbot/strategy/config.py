"""Versioned, strictly validated strategy configuration."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_UNIVERSE = (
    "SPY",
    "QQQ",
    "IWM",
    "MDY",
    "EFA",
    "EEM",
    "VNQ",
    "XLB",
    "XLE",
    "XLF",
    "XLI",
    "XLK",
    "XLP",
    "XLU",
    "XLV",
    "XLY",
    "DBC",
    "GLD",
    "TLT",
    "IEF",
    "TIP",
    "LQD",
    "HYG",
)


class StrategyConfig(BaseModel):
    """Immutable adaptive-momentum V1 configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_name: Literal["adaptive-momentum"]
    # 1.0.0 is whole-share only; 1.1.0 is the same signal logic with fractional shares.
    version: Literal["1.0.0", "1.1.0"]
    calendar: Literal["XNYS"]
    universe: tuple[str, ...]
    benchmark_symbol: str
    regime_symbol: str
    roster_size: int = Field(gt=0)
    momentum_long: int = Field(gt=0)
    momentum_skip: int = Field(ge=0)
    trend_period: int = Field(gt=0)
    entry_period: int = Field(gt=0)
    exit_period: int = Field(gt=0)
    atr_period: int = Field(gt=0)
    initial_stop_atr: Decimal = Field(gt=0, allow_inf_nan=False)
    trailing_stop_atr: Decimal = Field(gt=0, allow_inf_nan=False)
    rotation_frequency: Literal["monthly"]
    positive_momentum_required: bool
    market_regime_blocks_entries_only: bool
    risk_per_trade_bps: int = Field(gt=0)
    max_open_risk_bps: int = Field(gt=0)
    max_position_value_bps: int = Field(gt=0)
    max_gross_exposure_bps: int = Field(gt=0)
    max_positions: int = Field(gt=0)
    drawdown_thresholds_bps: tuple[int, int, int, int]
    drawdown_multipliers_bps: tuple[int, int, int, int, int]
    slippage_bps: int = Field(ge=0)
    commission_per_order: Decimal = Field(ge=0, allow_inf_nan=False)
    idle_cash_rate_bps: int = Field(ge=0)
    allow_fractional_shares: bool
    allow_pyramiding: bool

    @field_validator("universe")
    @classmethod
    def validate_universe(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("universe must not be empty")
        invalid_symbols = (
            not symbol or symbol != symbol.strip() or symbol != symbol.upper() for symbol in value
        )
        if any(invalid_symbols):
            raise ValueError("universe symbols must be nonempty uppercase values")
        if len(set(value)) != len(value):
            raise ValueError("universe symbols must be unique")
        if "SPY" not in value:
            raise ValueError("universe must include SPY")
        return value

    @field_validator("benchmark_symbol", "regime_symbol")
    @classmethod
    def validate_reference_symbol(cls, value: str) -> str:
        if not value or value != value.strip() or value != value.upper():
            raise ValueError("reference symbols must be nonempty uppercase values")
        return value

    @model_validator(mode="after")
    def validate_relationships(self) -> Self:
        if self.benchmark_symbol != "SPY" or self.regime_symbol != "SPY":
            raise ValueError("benchmark_symbol and regime_symbol must both be SPY for V1")
        if self.benchmark_symbol not in self.universe or self.regime_symbol not in self.universe:
            raise ValueError("benchmark and regime symbols must be in universe")
        if self.roster_size > len(self.universe):
            raise ValueError("roster_size cannot exceed universe size")
        if self.roster_size > self.max_positions:
            raise ValueError("roster_size cannot exceed max_positions")
        if self.momentum_skip >= self.momentum_long:
            raise ValueError("momentum_skip must be less than momentum_long")
        if self.exit_period >= self.entry_period:
            raise ValueError("exit_period must be less than entry_period")
        if self.trailing_stop_atr < self.initial_stop_atr:
            raise ValueError("trailing_stop_atr cannot be less than initial_stop_atr")
        if self.max_open_risk_bps < self.risk_per_trade_bps:
            raise ValueError("max_open_risk_bps cannot be less than risk_per_trade_bps")
        if self.max_position_value_bps > self.max_gross_exposure_bps:
            raise ValueError("max_position_value_bps cannot exceed max_gross_exposure_bps")
        if any(value <= 0 or value > 10000 for value in self.drawdown_thresholds_bps):
            raise ValueError("drawdown thresholds must be between 1 and 10000 bps")
        if tuple(sorted(self.drawdown_thresholds_bps)) != self.drawdown_thresholds_bps:
            raise ValueError("drawdown thresholds must be strictly increasing")
        if len(set(self.drawdown_thresholds_bps)) != len(self.drawdown_thresholds_bps):
            raise ValueError("drawdown thresholds must be unique")
        multipliers = self.drawdown_multipliers_bps
        if any(value < 0 or value > 10000 for value in multipliers):
            raise ValueError("drawdown multipliers must be between 0 and 10000 bps")
        if tuple(sorted(multipliers, reverse=True)) != multipliers:
            raise ValueError("drawdown multipliers must be nonincreasing")
        return self


def load_strategy_config(path: str | Path) -> StrategyConfig:
    """Load a YAML mapping with SafeLoader and reject all unknown or missing keys."""
    with Path(path).open(encoding="utf-8") as source:
        raw: Any = yaml.safe_load(source)
    if not isinstance(raw, dict):
        raise ValueError("strategy configuration must be a YAML mapping")
    return StrategyConfig.model_validate(raw)
