"""Deterministic monthly roster construction and daily signal decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quantbot.domain import Bar, StrategyIdentity
from quantbot.strategy.config import StrategyConfig
from quantbot.strategy.identity import bar_set_hash
from quantbot.strategy.indicators import (
    donchian_entry_level,
    donchian_exit_level,
    highest_high_since,
    momentum_12_1,
    sma,
    wilder_atr,
)


class StrategyModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def normalize_datetime(cls, value: object) -> object:
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)


class Ranking(StrategyModel):
    symbol: str
    momentum: Decimal
    close: Decimal
    sma200: Decimal

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        if not value or value != value.strip() or value != value.upper():
            raise ValueError("symbol must be uppercase")
        return value


class MonthlyRoster(StrategyModel):
    evaluated_at: datetime
    effective_at: datetime
    symbols: tuple[str, ...]
    rankings: tuple[Ranking, ...]

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("roster symbols must be unique")
        invalid_symbols = (
            not symbol or symbol != symbol.strip() or symbol != symbol.upper() for symbol in value
        )
        if any(invalid_symbols):
            raise ValueError("roster symbols must be uppercase")
        return value

    @model_validator(mode="after")
    def validate_times(self) -> MonthlyRoster:
        if self.effective_at <= self.evaluated_at:
            raise ValueError("effective_at must be after evaluated_at")
        return self


class SignalAction(StrEnum):
    ENTER = "ENTER"
    EXIT = "EXIT"
    HOLD = "HOLD"
    INELIGIBLE = "INELIGIBLE"


class SignalDecision(StrategyModel):
    symbol: str
    action: SignalAction
    reasons: tuple[str, ...]
    evaluated_at: datetime
    eligible_from: datetime
    momentum: Decimal | None
    sma200: Decimal | None
    entry_channel: Decimal | None
    exit_channel: Decimal | None
    atr: Decimal | None
    regime_on: bool
    active_roster: bool
    strategy_id: str
    configuration_hash: str
    input_cutoff: datetime
    bar_set_hash: str
    initial_stop_distance: Decimal | None = None
    trailing_stop: Decimal | None = None


class PositionContext(StrategyModel):
    symbol: str
    entered_at: datetime
    initial_stop: Decimal = Field(gt=0, allow_inf_nan=False)
    active_stop: Decimal = Field(gt=0, allow_inf_nan=False)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        if not value or value != value.strip() or value != value.upper():
            raise ValueError("symbol must be uppercase")
        return value


def _aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _sliced(history: Sequence[Bar], cutoff: datetime) -> list[Bar]:
    return [bar for bar in history if bar.timestamp <= cutoff]


def _validate_history(history: Sequence[Bar], expected_symbol: str) -> None:
    previous: datetime | None = None
    for bar in history:
        if bar.symbol != expected_symbol:
            raise ValueError(f"history must contain only {expected_symbol} bars")
        if previous is not None and bar.timestamp <= previous:
            raise ValueError("history bars must be strictly increasing without duplicates")
        previous = bar.timestamp


def build_monthly_roster(
    histories: Mapping[str, Sequence[Bar]],
    evaluation_at: datetime,
    effective_at: datetime,
    config: StrategyConfig,
) -> MonthlyRoster:
    """Rank eligible symbols using only bars available at evaluation time."""
    _aware("evaluation_at", evaluation_at)
    _aware("effective_at", effective_at)
    if effective_at <= evaluation_at:
        raise ValueError("effective_at must be after evaluation_at")
    unknown = sorted(set(histories) - set(config.universe))
    if unknown:
        raise ValueError(f"symbols not in strategy universe: {', '.join(unknown)}")

    rankings: list[Ranking] = []
    for symbol, history in histories.items():
        _validate_history(history, symbol)
        sliced = _sliced(history, evaluation_at)
        momentum = momentum_12_1(sliced, config.momentum_long, config.momentum_skip)
        trend = sma(sliced, config.trend_period)
        if momentum is None or trend is None or not sliced:
            continue
        close = sliced[-1].close
        if momentum <= 0 or close <= trend:
            continue
        rankings.append(Ranking(symbol=symbol, momentum=momentum, close=close, sma200=trend))

    ranked = sorted(rankings, key=lambda item: (-item.momentum, item.symbol))[
        : config.roster_size
    ]
    return MonthlyRoster(
        evaluated_at=evaluation_at,
        effective_at=effective_at,
        symbols=tuple(item.symbol for item in ranked),
        rankings=tuple(ranked),
    )


def evaluate_symbol(
    symbol_history: Sequence[Bar],
    spy_history: Sequence[Bar],
    roster: MonthlyRoster,
    evaluation_at: datetime,
    next_session_at: datetime,
    config: StrategyConfig,
    identity: StrategyIdentity,
    position: PositionContext | None = None,
) -> SignalDecision:
    """Evaluate one symbol at a strict cutoff for action at the next session."""
    _aware("evaluation_at", evaluation_at)
    _aware("next_session_at", next_session_at)
    if next_session_at <= evaluation_at:
        raise ValueError("next_session_at must be after evaluation_at")
    if not symbol_history:
        raise ValueError("symbol_history must not be empty")
    symbol = symbol_history[0].symbol
    _validate_history(symbol_history, symbol)
    _validate_history(spy_history, config.regime_symbol)
    if position is not None and position.symbol != symbol:
        raise ValueError("position symbol must match evaluated symbol")

    bars = _sliced(symbol_history, evaluation_at)
    spy_bars = _sliced(spy_history, evaluation_at)
    active_roster = symbol in roster.symbols and roster.effective_at <= evaluation_at
    inputs_hash = bar_set_hash({symbol: bars, config.regime_symbol: spy_bars}, evaluation_at)

    common = {
        "symbol": symbol,
        "evaluated_at": evaluation_at,
        "eligible_from": next_session_at,
        "active_roster": active_roster,
        "strategy_id": identity.strategy_id,
        "configuration_hash": identity.configuration_hash,
        "input_cutoff": evaluation_at,
        "bar_set_hash": inputs_hash,
    }
    current_bars_available = (
        bars
        and spy_bars
        and bars[-1].timestamp == evaluation_at
        and spy_bars[-1].timestamp == evaluation_at
    )
    if not current_bars_available:
        return SignalDecision(
            action=SignalAction.INELIGIBLE,
            reasons=("STALE_OR_MISSING_BAR",),
            momentum=None,
            sma200=None,
            entry_channel=None,
            exit_channel=None,
            atr=None,
            regime_on=False,
            **common,
        )

    momentum = momentum_12_1(bars, config.momentum_long, config.momentum_skip)
    trend = sma(bars, config.trend_period)
    spy_trend = sma(spy_bars, config.trend_period)
    entry_channel = donchian_entry_level(bars, config.entry_period)
    exit_channel = donchian_exit_level(bars, config.exit_period)
    atr = wilder_atr(bars, config.atr_period)
    if None in (momentum, trend, spy_trend, entry_channel, exit_channel, atr):
        return SignalDecision(
            action=SignalAction.INELIGIBLE,
            reasons=("WARMUP_INCOMPLETE",),
            momentum=momentum,
            sma200=trend,
            entry_channel=entry_channel,
            exit_channel=exit_channel,
            atr=atr,
            regime_on=False,
            **common,
        )

    assert momentum is not None
    assert trend is not None
    assert spy_trend is not None
    assert entry_channel is not None
    assert exit_channel is not None
    assert atr is not None
    close = bars[-1].close
    regime_on = spy_bars[-1].close > spy_trend

    if position is None:
        failed: list[str] = []
        if not active_roster:
            failed.append("NOT_IN_ACTIVE_ROSTER")
        if not regime_on:
            failed.append("REGIME_OFF")
        if close <= trend:
            failed.append("TREND_FILTER_FAILED")
        if momentum <= 0:
            failed.append("NON_POSITIVE_MOMENTUM")
        if close <= entry_channel:
            failed.append("ENTRY_CHANNEL_NOT_BROKEN")
        action = SignalAction.HOLD if failed else SignalAction.ENTER
        return SignalDecision(
            action=action,
            reasons=tuple(failed) if failed else ("ENTRY_BREAKOUT",),
            momentum=momentum,
            sma200=trend,
            entry_channel=entry_channel,
            exit_channel=exit_channel,
            atr=atr,
            regime_on=regime_on,
            initial_stop_distance=config.initial_stop_atr * atr if not failed else None,
            **common,
        )

    high_water = highest_high_since(bars, position.entered_at)
    calculated_trail = position.initial_stop
    if high_water is not None:
        calculated_trail = high_water - config.trailing_stop_atr * atr
    trailing_stop = max(position.initial_stop, position.active_stop, calculated_trail)
    exit_reasons: list[str] = []
    if not active_roster:
        exit_reasons.append("ROSTER_EXIT")
    if close <= trend:
        exit_reasons.append("TREND_EXIT")
    if close < exit_channel:
        exit_reasons.append("DONCHIAN_EXIT")
    if close <= trailing_stop:
        exit_reasons.append("TRAILING_STOP_EXIT")
    return SignalDecision(
        action=SignalAction.EXIT if exit_reasons else SignalAction.HOLD,
        reasons=tuple(exit_reasons) if exit_reasons else ("POSITION_HELD",),
        momentum=momentum,
        sma200=trend,
        entry_channel=entry_channel,
        exit_channel=exit_channel,
        atr=atr,
        regime_on=regime_on,
        trailing_stop=trailing_stop,
        **common,
    )
