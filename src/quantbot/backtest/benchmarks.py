"""Shared-indicator benchmark definitions and explicit full-strategy switches."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

from quantbot.domain import Bar
from quantbot.strategy import (
    StrategyConfig,
    donchian_entry_level,
    donchian_exit_level,
    momentum_12_1,
    sma,
)


class BenchmarkVariant(StrEnum):
    SPY_BUY_AND_HOLD = "SPY_BUY_AND_HOLD"
    SPY_SMA200 = "SPY_SMA200"
    SPY_DONCHIAN = "SPY_DONCHIAN"
    PURE_MOMENTUM_12_1 = "PURE_MOMENTUM_12_1"
    MOMENTUM_TREND = "MOMENTUM_TREND"
    FULL_STRATEGY = "FULL_STRATEGY"


class ComponentSwitches(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    momentum: bool = True
    asset_trend: bool = True
    market_regime: bool = True
    donchian_entry: bool = True
    donchian_exit: bool = True
    atr_risk: bool = True
    trailing_stop: bool = True
    roster_exit: bool = True

    @classmethod
    def full(cls) -> ComponentSwitches:
        return cls()


def component_switches_for(variant: BenchmarkVariant) -> ComponentSwitches:
    """Expose the exact components active in each comparator."""
    disabled = {
        "momentum": False,
        "asset_trend": False,
        "market_regime": False,
        "donchian_entry": False,
        "donchian_exit": False,
        "atr_risk": False,
        "trailing_stop": False,
        "roster_exit": False,
    }
    if variant is BenchmarkVariant.SPY_BUY_AND_HOLD:
        return ComponentSwitches(**disabled)
    if variant is BenchmarkVariant.SPY_SMA200:
        return ComponentSwitches(**{**disabled, "asset_trend": True})
    if variant is BenchmarkVariant.SPY_DONCHIAN:
        return ComponentSwitches(**{**disabled, "donchian_entry": True, "donchian_exit": True})
    if variant is BenchmarkVariant.PURE_MOMENTUM_12_1:
        return ComponentSwitches(**{**disabled, "momentum": True, "roster_exit": True})
    if variant is BenchmarkVariant.MOMENTUM_TREND:
        return ComponentSwitches(
            **{
                **disabled,
                "momentum": True,
                "asset_trend": True,
                "roster_exit": True,
            }
        )
    return ComponentSwitches.full()


class AssetRanking(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    momentum: Decimal

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        if not value or value != value.strip() or value != value.upper():
            raise ValueError("ranking symbol must be uppercase")
        return value


def rank_assets(
    histories: Mapping[str, Sequence[Bar]],
    config: StrategyConfig,
    *,
    require_trend: bool,
    require_positive: bool,
    use_momentum: bool = True,
    as_of: datetime | None = None,
) -> tuple[AssetRanking, ...]:
    if as_of is not None:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        as_of = as_of.astimezone(UTC)
    rankings: list[AssetRanking] = []
    for symbol in config.universe:
        bars = histories.get(symbol, ())
        if not bars:
            continue
        if as_of is not None and bars[-1].timestamp != as_of:
            continue
        momentum = momentum_12_1(bars, config.momentum_long, config.momentum_skip)
        trend = sma(bars, config.trend_period)
        if use_momentum and momentum is None:
            continue
        score = momentum if momentum is not None and use_momentum else Decimal("0")
        if require_positive and score <= 0:
            continue
        if require_trend and (trend is None or bars[-1].close <= trend):
            continue
        rankings.append(AssetRanking(symbol=symbol, momentum=score))
    return tuple(sorted(rankings, key=lambda item: (-item.momentum, item.symbol)))


def equal_weight_targets(
    rankings: Sequence[AssetRanking],
    config: StrategyConfig,
) -> dict[str, Decimal]:
    selected = tuple(rankings[: config.roster_size])
    if not selected:
        return {}
    equal_weight = Decimal("1") / Decimal(config.roster_size)
    capped_weight = min(equal_weight, Decimal("0.10"))
    return {ranking.symbol: capped_weight for ranking in selected}


def spy_target(
    variant: BenchmarkVariant,
    history: Sequence[Bar],
    config: StrategyConfig,
    *,
    currently_held: bool,
) -> dict[str, Decimal]:
    if variant is BenchmarkVariant.SPY_BUY_AND_HOLD:
        return {config.benchmark_symbol: Decimal("1")}
    if variant is BenchmarkVariant.SPY_SMA200:
        trend = sma(history, config.trend_period)
        return (
            {config.benchmark_symbol: Decimal("1")}
            if trend is not None and history[-1].close > trend
            else {}
        )
    if variant is not BenchmarkVariant.SPY_DONCHIAN:
        raise ValueError("spy_target supports only SPY benchmark variants")
    if currently_held:
        exit_level = donchian_exit_level(history, config.exit_period)
        return (
            {}
            if exit_level is not None and history[-1].close < exit_level
            else {config.benchmark_symbol: Decimal("1")}
        )
    entry_level = donchian_entry_level(history, config.entry_period)
    return (
        {config.benchmark_symbol: Decimal("1")}
        if entry_level is not None and history[-1].close > entry_level
        else {}
    )
