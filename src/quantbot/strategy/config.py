"""Versioned, strictly validated strategy configuration."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quantbot.domain import TradingCalendar

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


class StrategyComponents(BaseModel):
    """Which signal gates are active.

    The backtest engine has always been able to switch these; the live strategy could not,
    so research could recommend a configuration the account was unable to trade. Every
    field defaults to enabled, so an existing configuration behaves exactly as before.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    momentum: bool = True
    asset_trend: bool = True
    market_regime: bool = True
    donchian_entry: bool = True
    donchian_exit: bool = True
    trailing_stop: bool = True
    roster_exit: bool = True


class StrategyConfig(BaseModel):
    """Immutable adaptive-momentum V1 configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_name: Literal["adaptive-momentum"]
    # 1.0.0 whole-share only; 1.1.0 adds fractional shares; 1.2.0 allows the regime symbol
    # to sit outside the tradeable universe, which is what an asset-class sleeve needs;
    # 2.0.0 lifts the SPY-only benchmark so a non-equity market can define its own regime.
    version: Literal["1.0.0", "1.1.0", "1.2.0", "1.3.0", "2.0.0"]
    calendar: TradingCalendar
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
    #: Volatility-targeted sizing. 0 disables it, which is what every version through 1.2.0
    #: deployed with, so the default keeps their configuration hashes byte-identical.
    #:
    #: Cycle 12 measured why this is worth having, and it is NOT alpha: under this project's
    #: 20% drawdown halt, vol targeting carries 0.75x exposure where raw SPY affords only
    #: 0.50x, ending at $403.67 against $281.02 per $100. It buys back exposure that the
    #: drawdown rule would otherwise forbid. Unconstrained it costs ~1.83 CAGR points a year.
    volatility_target_bps: int = Field(default=0, ge=0)
    volatility_lookback_days: int = Field(default=20, gt=1)
    #: Staggered rebalance tranches. 1 is month-end rebalancing, which is what every version
    #: through 1.2.0 deployed, so the default keeps their configuration hashes byte-identical.
    #:
    #: Cycle 13 measured the cost of leaving this at 1: running the identical strategy on a
    #: different day of the month produced terminal wealth from $246.38 to $307.61 per $100, a
    #: $61.24 spread decided by nothing but the calendar. Tranching does not raise the expected
    #: return, it removes that gamble. 5 removes 88% of the dispersion; 21 removes all of it but
    #: needs $0.48 orders at a $100 account, below the broker minimum.
    rebalance_tranches: int = Field(default=1, ge=1, le=21)
    #: Size positions to a target weight instead of to an ATR-derived risk budget.
    #: False is what every version through 1.3.0 deployed, so the default keeps their
    #: configuration hashes byte-identical.
    #:
    #: Cycle 16 established why this is needed and it is architectural, not a preference.
    #: The highest-Sharpe mechanism measured in this project is "hold SPY while it is above
    #: its 200-day average" (10.34% CAGR, Sharpe 0.91, 77% exposure). Two configs failed to
    #: reproduce it because ATR-derived sizing always bound first, capping exposure at 30%
    #: and 43%. A rule that holds a fixed weight while a condition is true cannot be
    #: expressed through a risk budget divided by a stop distance.
    target_weight_sizing: bool = False
    #: Evaluate the trend condition per session instead of only when the roster is built.
    #: False is what every deployed version does, so the default keeps their hashes identical.
    #:
    #: Cycle 16: the trend filter at roster construction locks a below-trend symbol out for a
    #: whole month, so the engine is invested on 43% of sessions where SPY_SMA200 is invested
    #: on 77% using the same condition. Selection by momentum rank is legitimately monthly; a
    #: trend condition is a hold-while-true predicate and belongs in the per-session path,
    #: where the asset_trend component already runs.
    trend_gate_per_session: bool = False
    #: How many sessions of equity history define the high-water mark. 0 means all-time, which
    #: is what every deployed version uses, so the default keeps their hashes identical.
    #:
    #: Fixes an absorbing halt. With an all-time peak, a 15% drawdown blocks entries via
    #: entry_halted, and a strategy that cannot open positions cannot earn back the drawdown —
    #: so the halt is caused by a loss and then prevents the only thing that could undo it.
    #: Measured: the gate blocked entry on 70.8% of backtest sessions. A finite window lets an
    #: old peak age out, so time alone can release the account instead of requiring a deposit
    #: or manual reset. 504 sessions is about two years.
    drawdown_lookback_sessions: int = Field(default=0, ge=0)
    components: StrategyComponents = StrategyComponents()

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
        return value

    @field_validator("benchmark_symbol", "regime_symbol")
    @classmethod
    def validate_reference_symbol(cls, value: str) -> str:
        if not value or value != value.strip() or value != value.upper():
            raise ValueError("reference symbols must be nonempty uppercase values")
        return value

    @model_validator(mode="after")
    def validate_relationships(self) -> Self:
        # V1 pinned the benchmark to SPY, which makes a non-equity market inexpressible:
        # evaluate_symbol always requires regime-symbol bars, so a crypto sleeve forced to
        # use SPY would have to mix a 24/7 calendar with XNYS sessions. 2.0.0 lets a market
        # define its own regime instead, and the calendar constrains which is sensible.
        if self.version != "2.0.0" and (
            self.benchmark_symbol != "SPY" or self.regime_symbol != "SPY"
        ):
            raise ValueError("benchmark_symbol and regime_symbol must both be SPY for V1")
        if self.version == "2.0.0" and self.calendar == "XNYS" and self.regime_symbol != "SPY":
            raise ValueError("an XNYS strategy must use SPY as its regime symbol")
        # Before 1.2.0 the regime symbol had to be tradeable, which made an asset-class
        # sleeve inexpressible: a bond sleeve measures the equity regime without holding it.
        # Callers must supply regime history separately when it is outside the universe.
        if self.version in {"1.0.0", "1.1.0"} and self.regime_symbol not in self.universe:
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
