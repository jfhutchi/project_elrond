"""Deterministic monthly roster construction and daily signal decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quantbot.domain import Bar, StrategyIdentity, TradingCalendar
from quantbot.strategy.config import StrategyConfig
from quantbot.strategy.identity import bar_set_hash, validate_strategy_identity
from quantbot.strategy.indicators import (
    donchian_entry_level,
    donchian_exit_level,
    highest_high_since,
    momentum_12_1,
    sma,
    wilder_atr,
)
from quantbot.strategy.tranches import next_rebalance_index, tranche_schedule


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


class XNYSSession(StrategyModel):
    """One authoritative XNYS regular trading session."""

    session_date: date
    open_at: datetime
    close_at: datetime

    @model_validator(mode="after")
    def validate_bounds(self) -> XNYSSession:
        if self.close_at <= self.open_at:
            raise ValueError("session close_at must be after open_at")
        if self.open_at.date() != self.session_date or self.close_at.date() != self.session_date:
            raise ValueError("session boundaries must match session_date")
        return self


class XNYSSessionSequence(StrategyModel):
    """Caller-supplied contiguous session sequence from an XNYS calendar provider."""

    calendar: TradingCalendar
    sessions: tuple[XNYSSession, ...]

    @model_validator(mode="after")
    def validate_order(self) -> XNYSSessionSequence:
        if len(self.sessions) < 2:
            raise ValueError("XNYS session sequence must contain at least two sessions")
        for previous, current in zip(self.sessions, self.sessions[1:], strict=False):
            dates_not_increasing = current.session_date <= previous.session_date
            sessions_overlap = current.open_at <= previous.close_at
            if dates_not_increasing or sessions_overlap:
                raise ValueError("XNYS sessions must be strictly increasing and nonoverlapping")
        return self

    def transition_index(self, evaluation_at: datetime, next_session_at: datetime) -> int:
        """Return the evaluation-session index for an immediate close-to-open transition."""
        for index, session in enumerate(self.sessions[:-1]):
            if session.close_at != evaluation_at:
                continue
            if self.sessions[index + 1].open_at != next_session_at:
                raise ValueError("next_session_at must be the immediate next XNYS session")
            return index
        raise ValueError("evaluation_at must be an XNYS session close")

    def tranche_schedule_map(self, tranches: int) -> dict[int, int]:
        """Which tranche rebalances on each session index.

        With one tranche this is exactly the final session of every month, so today's
        month-end rule is the degenerate case rather than a separate code path.
        """
        return tranche_schedule(
            [session.session_date.strftime("%Y-%m") for session in self.sessions], tranches
        )

    def tranche_expiry_after(
        self, evaluation_index: int, *, tranche: int, tranches: int
    ) -> datetime:
        """When a roster built at `evaluation_index` for `tranche` stops governing.

        A roster lives until its own tranche next rebalances. For a single tranche that is the
        month boundary, so this returns exactly what `roster_expiry_after` does — verified by
        test rather than asserted.
        """
        schedule = self.tranche_schedule_map(tranches)
        next_index = next_rebalance_index(schedule, evaluation_index, tranche)
        if next_index is None or next_index + 1 >= len(self.sessions):
            raise ValueError("session sequence must extend past this tranche's next rebalance")
        return self.sessions[next_index + 1].open_at

    def roster_expiry_after(self, effective_index: int) -> datetime:
        """Return the first XNYS open after the effective session's calendar month."""
        effective_month = self.sessions[effective_index].session_date.strftime("%Y-%m")
        for session in self.sessions[effective_index + 1 :]:
            if session.session_date.strftime("%Y-%m") != effective_month:
                return session.open_at
        raise ValueError("session sequence must include the first session after effective month")

    def validate_roster_window(
        self,
        roster: MonthlyRoster,
        next_session_at: datetime,
    ) -> None:
        """Prove the roster is the month-bound roster governing the target session."""
        if roster.calendar != self.calendar:
            raise ValueError("roster schedule calendar does not match session sequence")
        effective_index = next(
            (
                index
                for index, session in enumerate(self.sessions)
                if session.open_at == roster.effective_at
            ),
            None,
        )
        if effective_index is None or effective_index == 0:
            raise ValueError("roster schedule is not represented by the session sequence")
        prior_session = self.sessions[effective_index - 1]
        effective_session = self.sessions[effective_index]
        prior_month = prior_session.session_date.strftime("%Y-%m")
        effective_month = effective_session.session_date.strftime("%Y-%m")
        if prior_month == effective_month or prior_session.close_at != roster.evaluated_at:
            raise ValueError("roster schedule must start at the first XNYS session of a month")
        if roster.expires_at != self.roster_expiry_after(effective_index):
            raise ValueError("roster schedule expires_at does not match the XNYS month boundary")
        if not (roster.effective_at <= next_session_at < roster.expires_at):
            raise ValueError("roster is not effective for next_session_at")


class MonthlyRoster(StrategyModel):
    calendar: TradingCalendar
    evaluated_at: datetime
    effective_at: datetime
    expires_at: datetime
    symbols: tuple[str, ...]
    rankings: tuple[Ranking, ...]
    #: Which tranche produced this roster, and how many are running. The defaults describe an
    #: untranched strategy, which is every deployed version. Exits cannot be attributed without
    #: this, and a mis-attributed exit surfaces as a position diff that halts trading.
    tranche: int = 0
    tranche_count: int = 1

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
        if self.expires_at <= self.effective_at:
            raise ValueError("expires_at must be after effective_at")
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
    asset_close: Decimal | None
    spy_close: Decimal | None
    spy_sma200: Decimal | None
    high_water_since_entry: Decimal | None
    prior_active_stop: Decimal | None
    initial_stop_distance: Decimal | None = None
    trailing_stop: Decimal | None = None


class PositionContext(StrategyModel):
    symbol: str
    entered_at: datetime
    initial_stop: Decimal = Field(gt=0, allow_inf_nan=False)
    active_stop: Decimal = Field(gt=0, allow_inf_nan=False)
    #: Which tranche opened this position. Under tranching the same symbol can be held by
    #: several tranches at once with different entry dates and different stops, so an exit has
    #: to name the tranche it belongs to. Mis-attributing one shows up as a broker position
    #: diff, which fails reconciliation and halts trading — the expensive failure this field
    #: exists to prevent. 0 is the untranched case and is what every deployed version means.
    tranche: int = Field(default=0, ge=0)

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
    *,
    session_sequence: XNYSSessionSequence,
    tranche: int = 0,
) -> MonthlyRoster:
    """Rank eligible symbols using only bars available at evaluation time.

    `tranche` selects which rebalance schedule the evaluation date must belong to. At the
    default single tranche the schedule is exactly the month-end sessions, so the guard below
    is the same rule it has always been, expressed once instead of twice.
    """
    _aware("evaluation_at", evaluation_at)
    _aware("effective_at", effective_at)
    if effective_at <= evaluation_at:
        raise ValueError("effective_at must be after evaluation_at")
    if session_sequence.calendar != config.calendar:
        raise ValueError("session calendar must match strategy configuration")
    evaluation_index = session_sequence.transition_index(evaluation_at, effective_at)
    evaluation_session = session_sequence.sessions[evaluation_index]
    effective_index = evaluation_index + 1
    effective_session = session_sequence.sessions[effective_index]
    tranches = config.rebalance_tranches
    if tranches == 1:
        # Kept as an explicit branch rather than folded into the schedule check so the error
        # message every deployed version can raise stays byte-identical.
        if evaluation_session.session_date.strftime(
            "%Y-%m"
        ) == effective_session.session_date.strftime("%Y-%m"):
            raise ValueError("evaluation_at must be the final XNYS session of its month")
        expires_at = session_sequence.roster_expiry_after(effective_index)
    else:
        schedule = session_sequence.tranche_schedule_map(tranches)
        if schedule.get(evaluation_index) != tranche:
            raise ValueError(
                f"evaluation_at is not tranche {tranche}'s rebalance session"
            )
        expires_at = session_sequence.tranche_expiry_after(
            evaluation_index, tranche=tranche, tranches=tranches
        )

    expected_symbols = set(config.universe)
    provided_symbols = set(histories)
    if provided_symbols != expected_symbols:
        missing = sorted(expected_symbols - provided_symbols)
        unexpected = sorted(provided_symbols - expected_symbols)
        detail = f"missing={missing}, unexpected={unexpected}"
        raise ValueError(f"histories must exactly match strategy universe: {detail}")

    rankings: list[Ranking] = []
    for symbol in config.universe:
        history = histories[symbol]
        _validate_history(history, symbol)
        sliced = _sliced(history, evaluation_at)
        if not sliced or sliced[-1].timestamp != evaluation_at:
            raise ValueError(f"history for {symbol} must be fresh through evaluation_at")
        momentum = momentum_12_1(sliced, config.momentum_long, config.momentum_skip)
        trend = sma(sliced, config.trend_period)
        if momentum is None or trend is None:
            continue
        close = sliced[-1].close
        below_trend = close <= trend
        if config.trend_gate_per_session:
            # Eligibility only. The trend condition is re-checked every session by the
            # asset_trend component, so excluding here would lock a symbol out for a whole
            # month over one month-end close and cost most of the return (cycle 16).
            below_trend = False
        if (config.positive_momentum_required and momentum <= 0) or below_trend:
            continue
        rankings.append(Ranking(symbol=symbol, momentum=momentum, close=close, sma200=trend))

    ranked = sorted(rankings, key=lambda item: (-item.momentum, item.symbol))[: config.roster_size]
    return MonthlyRoster(
        calendar=config.calendar,
        evaluated_at=evaluation_at,
        effective_at=effective_at,
        expires_at=expires_at,
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
    *,
    session_sequence: XNYSSessionSequence,
) -> SignalDecision:
    """Evaluate one symbol at a strict cutoff for action at the next session."""
    _aware("evaluation_at", evaluation_at)
    _aware("next_session_at", next_session_at)
    if next_session_at <= evaluation_at:
        raise ValueError("next_session_at must be after evaluation_at")
    validate_strategy_identity(config, identity)
    if session_sequence.calendar != config.calendar:
        raise ValueError("session calendar must match strategy configuration")
    session_sequence.transition_index(evaluation_at, next_session_at)
    session_sequence.validate_roster_window(roster, next_session_at)
    if not symbol_history:
        raise ValueError("symbol_history must not be empty")
    symbol = symbol_history[0].symbol
    if symbol not in config.universe:
        raise ValueError("evaluated symbol must be in strategy universe")
    if any(roster_symbol not in config.universe for roster_symbol in roster.symbols):
        raise ValueError("roster symbols must be in strategy universe")
    _validate_history(symbol_history, symbol)
    _validate_history(spy_history, config.regime_symbol)
    if position is not None and position.symbol != symbol:
        raise ValueError("position symbol must match evaluated symbol")

    bars = _sliced(symbol_history, evaluation_at)
    spy_bars = _sliced(spy_history, evaluation_at)
    active_roster = symbol in roster.symbols
    inputs_hash = bar_set_hash({symbol: bars, config.regime_symbol: spy_bars}, evaluation_at)

    asset_current = bars[-1] if bars and bars[-1].timestamp == evaluation_at else None
    spy_current = spy_bars[-1] if spy_bars and spy_bars[-1].timestamp == evaluation_at else None
    spy_trend = sma(spy_bars, config.trend_period) if spy_current is not None else None

    def make_decision(
        action: SignalAction,
        reasons: tuple[str, ...],
        *,
        momentum: Decimal | None = None,
        trend: Decimal | None = None,
        entry_channel: Decimal | None = None,
        exit_channel: Decimal | None = None,
        atr: Decimal | None = None,
        regime_on: bool = False,
        high_water: Decimal | None = None,
        initial_stop_distance: Decimal | None = None,
        trailing_stop: Decimal | None = None,
    ) -> SignalDecision:
        return SignalDecision(
            symbol=symbol,
            action=action,
            reasons=reasons,
            evaluated_at=evaluation_at,
            eligible_from=next_session_at,
            momentum=momentum,
            sma200=trend,
            entry_channel=entry_channel,
            exit_channel=exit_channel,
            atr=atr,
            regime_on=regime_on,
            active_roster=active_roster,
            strategy_id=identity.strategy_id,
            configuration_hash=identity.configuration_hash,
            input_cutoff=evaluation_at,
            bar_set_hash=inputs_hash,
            asset_close=asset_current.close if asset_current is not None else None,
            spy_close=spy_current.close if spy_current is not None else None,
            spy_sma200=spy_trend,
            high_water_since_entry=high_water,
            prior_active_stop=position.active_stop if position is not None else None,
            initial_stop_distance=initial_stop_distance,
            trailing_stop=trailing_stop,
        )

    if asset_current is None or spy_current is None:
        return make_decision(SignalAction.INELIGIBLE, ("STALE_OR_MISSING_BAR",))

    momentum = momentum_12_1(bars, config.momentum_long, config.momentum_skip)
    trend = sma(bars, config.trend_period)
    entry_channel = donchian_entry_level(bars, config.entry_period)
    exit_channel = donchian_exit_level(bars, config.exit_period)
    atr = wilder_atr(bars, config.atr_period)
    close = asset_current.close
    regime_on = spy_trend is not None and spy_current.close > spy_trend

    if position is None:
        if None in (momentum, trend, spy_trend, entry_channel, exit_channel, atr):
            return make_decision(
                SignalAction.INELIGIBLE,
                ("WARMUP_INCOMPLETE",),
                momentum=momentum,
                trend=trend,
                entry_channel=entry_channel,
                exit_channel=exit_channel,
                atr=atr,
                regime_on=regime_on,
            )
        assert momentum is not None
        assert trend is not None
        assert entry_channel is not None
        assert exit_channel is not None
        assert atr is not None
        components = config.components
        failed: list[str] = []
        if components.momentum and not active_roster:
            failed.append("NOT_IN_ACTIVE_ROSTER")
        if components.market_regime and config.market_regime_blocks_entries_only and not regime_on:
            failed.append("REGIME_OFF")
        if components.asset_trend and close <= trend:
            failed.append("TREND_FILTER_FAILED")
        if components.momentum and config.positive_momentum_required and momentum <= 0:
            failed.append("NON_POSITIVE_MOMENTUM")
        if components.donchian_entry and close <= entry_channel:
            failed.append("ENTRY_CHANNEL_NOT_BROKEN")
        action = SignalAction.HOLD if failed else SignalAction.ENTER
        return make_decision(
            action,
            tuple(failed) if failed else ("ENTRY_BREAKOUT",),
            momentum=momentum,
            trend=trend,
            entry_channel=entry_channel,
            exit_channel=exit_channel,
            atr=atr,
            regime_on=regime_on,
            initial_stop_distance=config.initial_stop_atr * atr if not failed else None,
        )

    high_water = highest_high_since(bars, position.entered_at)
    calculated_trail = position.initial_stop
    if high_water is not None and atr is not None:
        calculated_trail = high_water - config.trailing_stop_atr * atr
    trailing_stop = max(position.initial_stop, position.active_stop, calculated_trail)
    components = config.components
    exit_reasons: list[str] = []
    if components.roster_exit and not active_roster:
        exit_reasons.append("ROSTER_EXIT")
    if components.asset_trend and trend is not None and close <= trend:
        exit_reasons.append("TREND_EXIT")
    if components.donchian_exit and exit_channel is not None and close < exit_channel:
        exit_reasons.append("DONCHIAN_EXIT")
    if components.trailing_stop and close <= trailing_stop:
        exit_reasons.append("TRAILING_STOP_EXIT")

    exit_warmup_incomplete = trend is None or exit_channel is None or atr is None
    if exit_warmup_incomplete:
        if exit_reasons:
            exit_reasons.append("EXIT_WARMUP_INCOMPLETE")
            action = SignalAction.EXIT
            reasons = tuple(exit_reasons)
        else:
            action = SignalAction.INELIGIBLE
            reasons = ("EXIT_WARMUP_INCOMPLETE",)
    else:
        action = SignalAction.EXIT if exit_reasons else SignalAction.HOLD
        reasons = tuple(exit_reasons) if exit_reasons else ("POSITION_HELD",)

    return make_decision(
        action,
        reasons,
        momentum=momentum,
        trend=trend,
        entry_channel=entry_channel,
        exit_channel=exit_channel,
        atr=atr,
        regime_on=regime_on,
        high_water=high_water,
        trailing_stop=trailing_stop,
    )
