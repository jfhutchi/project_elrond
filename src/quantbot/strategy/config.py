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
    version: Literal["1.0.0"]
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
    positive_momentum_required: Literal[True]
    market_regime_blocks_entries_only: Literal[True]

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
        if self.momentum_skip >= self.momentum_long:
            raise ValueError("momentum_skip must be less than momentum_long")
        if self.exit_period >= self.entry_period:
            raise ValueError("exit_period must be less than entry_period")
        if self.trailing_stop_atr < self.initial_stop_atr:
            raise ValueError("trailing_stop_atr cannot be less than initial_stop_atr")
        return self


def load_strategy_config(path: str | Path) -> StrategyConfig:
    """Load a YAML mapping with SafeLoader and reject all unknown or missing keys."""
    with Path(path).open(encoding="utf-8") as source:
        raw: Any = yaml.safe_load(source)
    if not isinstance(raw, dict):
        raise ValueError("strategy configuration must be a YAML mapping")
    return StrategyConfig.model_validate(raw)
