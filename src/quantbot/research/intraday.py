"""Offline, exploratory research of the frozen 30-minute opening-range breakout.

Bar timestamps denote interval starts. Intrabar stops are booked at interval end,
because OHLC does not identify the actual touch time. Their proceeds cannot fund
entries at that interval's open. Completeness is an explicit ex-post sample filter,
not a price-based universe filter. Callers supply authoritative exchange sessions.
This is not a strategy promotion gate or a broker execution implementation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_DOWN, Context, Decimal, localcontext
from typing import Literal
from zoneinfo import ZoneInfo

from quantbot.domain import Bar
from quantbot.domain.models import FrozenModel
from quantbot.strategy.adaptive_momentum import XNYSSession

UNIVERSE = ("SPY", "QQQ", "IWM", "EFA", "EEM", "XLE", "XLF", "XLK", "XLI", "XLV")
STEP = timedelta(minutes=15)
HOLD = timedelta(minutes=90)
NY = ZoneInfo("America/New_York")
ZERO = Decimal("0")
ONE = Decimal("1")
ALLOCATION = Decimal("0.09")
TOTAL_ALLOCATION = Decimal("0.90")
RISK = Decimal("0.005")
QUANTUM = Decimal("0.000001")
ExitReason = Literal["STOP", "MAX_HOLD", "SESSION_EXIT"]


class IntradayTrade(FrozenModel):
    symbol: str
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    net_pnl: Decimal
    reason: ExitReason


class IntradaySessionResult(FrozenModel):
    session_date: date
    starting_equity: Decimal
    equity: Decimal
    net_return: Decimal
    included_symbols: tuple[str, ...]
    excluded_symbols: tuple[str, ...]


class SkippedSymbolDay(FrozenModel):
    symbol: str
    session_date: date
    reason: Literal["INCOMPLETE_SESSION", "ENTRY_AT_OR_BELOW_STOP", "SIZE_BELOW_PRECISION"]
    expected_bars: int
    observed_bars: int


class IntradayStats(FrozenModel):
    net_return: Decimal
    max_drawdown: Decimal
    trade_count: int
    winning_trades: int


class IntradayResult(FrozenModel):
    status: Literal["EXPLORATORY_ONLY"] = "EXPLORATORY_ONLY"
    qualified: Literal[False] = False
    input_hash: str
    starting_cash: Decimal
    slippage_bps: int
    trades: tuple[IntradayTrade, ...]
    sessions: tuple[IntradaySessionResult, ...]
    skipped_symbol_days: tuple[SkippedSymbolDay, ...]
    stats: IntradayStats
    assumptions: tuple[str, ...] = (
        "Raw interval-start 15-minute OHLC; caller supplies authoritative XNYS calendar.",
        "Incomplete symbol-days excluded using an ex-post completeness filter.",
        "Intrabar stops booked at bar end; exact touch time is unknown.",
        "Fixed adverse slippage each side; no liquidity, queue or broker fill guarantee.",
        "Drawdown sampled at portfolio bar opens/closes; intrabar path is unknown.",
        "Risk cap is initial stop risk including costs; gaps can exceed that risk.",
        "Deterministic fixed-universe order resolves simultaneous entry allocation.",
    )


@dataclass(frozen=True)
class _Position:
    entry_time: datetime
    entry_price: Decimal
    quantity: Decimal
    stop: Decimal


def _validated_inputs(
    histories: Mapping[str, Sequence[Bar]],
    sessions: Sequence[XNYSSession],
) -> tuple[dict[str, tuple[Bar, ...]], tuple[XNYSSession, ...]]:
    if set(histories) - set(UNIVERSE):
        raise ValueError("histories contain a symbol outside the frozen universe")
    validated_sessions = tuple(XNYSSession.model_validate(s.model_dump()) for s in sessions)
    if not validated_sessions:
        raise ValueError("sessions must not be empty")
    for i, s in enumerate(validated_sessions):
        if (s.close_at - s.open_at) % STEP or s.close_at - s.open_at < STEP * 4:
            raise ValueError("sessions must contain at least four complete 15-minute intervals")
        if s.open_at.astimezone(NY).time() != time(9, 30):
            raise ValueError("XNYS regular session must open at 09:30 New York time")
        if i and s.open_at <= validated_sessions[i - 1].close_at:
            raise ValueError("sessions must be strictly increasing and nonoverlapping")
    validated: dict[str, tuple[Bar, ...]] = {}
    for symbol in UNIVERSE:
        # Revalidate copies as model_copy/model_construct can bypass domain validators.
        items = tuple(Bar.model_validate(b.model_dump()) for b in histories.get(symbol, ()))
        for index, bar in enumerate(items):
            if bar.symbol != symbol:
                raise ValueError("history key and bar symbol must match")
            if bar.adjustment != ONE:
                raise ValueError("intraday research requires raw unadjusted bars")
            if index and bar.timestamp <= items[index - 1].timestamp:
                raise ValueError("bar timestamps must be strictly increasing without duplicates")
        validated[symbol] = items
    return validated, validated_sessions


def simulate(
    histories: Mapping[str, Sequence[Bar]],
    sessions: Sequence[XNYSSession],
    *,
    starting_cash: Decimal = Decimal("500"),
    slippage_bps: int = 5,
) -> IntradayResult:
    """Simulate one frozen candidate, with 5bps baseline or 10bps stress costs.

    Quantities round down to six decimals. Costs are included in fill prices,
    allocation, stop-risk sizing, cash and P&L. All positions share one cash ledger.
    Entry fills are allowed through 14:30 NY, strictly before the final bar open.
    """
    if (
        not isinstance(starting_cash, Decimal)
        or not starting_cash.is_finite()
        or starting_cash <= 0
    ):
        raise ValueError("starting_cash must be a positive finite Decimal")
    if type(slippage_bps) is not int or slippage_bps not in (5, 10):
        raise ValueError("slippage_bps must be frozen baseline 5 or prespecified stress 10")
    validated, calendar = _validated_inputs(histories, sessions)
    payload = {
        "version": "intraday-orb-v1",
        "config": {
            "universe": UNIVERSE,
            "range_minutes": 30,
            "bar_minutes": 15,
            "hold_minutes": 90,
            "last_entry_ny": "14:30",
            "long_only": True,
            "allocation": str(ALLOCATION),
            "total_allocation": str(TOTAL_ALLOCATION),
            "risk": str(RISK),
            "quantity_quantum": str(QUANTUM),
            "starting_cash": str(starting_cash),
            "slippage_bps": slippage_bps,
            "one_trade_per_symbol_session": True,
            "exit_final_bar_open": True,
            "complete_sessions_only": True,
            "decimal_precision": 28,
        },
        "sessions": [s.model_dump(mode="json") for s in calendar],
        "histories": {
            symbol: [b.model_dump(mode="json") for b in values]
            for symbol, values in validated.items()
        },
    }
    input_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with localcontext(Context(prec=28)):
        return _run(validated, calendar, starting_cash, slippage_bps, input_hash)


def _run(
    histories: dict[str, tuple[Bar, ...]],
    sessions: tuple[XNYSSession, ...],
    starting_cash: Decimal,
    slippage_bps: int,
    input_hash: str,
) -> IntradayResult:
    cost = Decimal(slippage_bps) / Decimal("10000")
    cash = starting_cash
    peak = starting_cash
    max_drawdown = ZERO
    trades: list[IntradayTrade] = []
    session_results: list[IntradaySessionResult] = []
    skipped: list[SkippedSymbolDay] = []
    positions: dict[str, _Position] = {}
    marks: dict[str, Decimal] = {}

    def drawdown(equity: Decimal) -> None:
        nonlocal peak, max_drawdown
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)

    def equity() -> Decimal:
        return cash + sum((p.quantity * marks[s] for s, p in positions.items()), ZERO)

    def close(symbol: str, price: Decimal, at: datetime, reason: ExitReason) -> None:
        nonlocal cash
        position = positions.pop(symbol)
        fill = price * (ONE - cost)
        cash += fill * position.quantity
        trades.append(
            IntradayTrade(
                symbol=symbol,
                entry_time=position.entry_time,
                exit_time=at,
                entry_price=position.entry_price,
                exit_price=fill,
                quantity=position.quantity,
                net_pnl=(fill - position.entry_price) * position.quantity,
                reason=reason,
            )
        )

    for session in sessions:
        starting_equity = cash
        times = tuple(
            session.open_at + STEP * i
            for i in range(int((session.close_at - session.open_at) / STEP))
        )
        complete: dict[str, tuple[Bar, ...]] = {}
        for symbol in UNIVERSE:
            daily = tuple(
                b for b in histories[symbol] if session.open_at <= b.timestamp < session.close_at
            )
            if tuple(b.timestamp for b in daily) != times:
                skipped.append(
                    SkippedSymbolDay(
                        symbol=symbol,
                        session_date=session.session_date,
                        reason="INCOMPLETE_SESSION",
                        expected_bars=len(times),
                        observed_bars=len(daily),
                    )
                )
            else:
                complete[symbol] = daily

        attempted: set[str] = set()

        for index, at in enumerate(times):
            # All opens are simultaneous observations; no high/low/close of this bar yet.
            marks = {symbol: values[index].open for symbol, values in complete.items()}

            drawdown(equity())
            for symbol, position in list(positions.items()):
                if marks[symbol] <= position.stop:
                    close(symbol, marks[symbol], at, "STOP")
                elif index == len(times) - 1:
                    close(symbol, marks[symbol], at, "SESSION_EXIT")
                elif at - position.entry_time >= HOLD:
                    close(symbol, marks[symbol], at, "MAX_HOLD")

            if index >= 3 and index < len(times) - 1 and at.astimezone(NY).time() <= time(14, 30):
                for symbol, values in complete.items():
                    if symbol in attempted:
                        continue
                    range_high = max(values[0].high, values[1].high)
                    if values[index - 1].close <= range_high:
                        continue
                    attempted.add(symbol)
                    stop = min(values[0].low, values[1].low)
                    entry_price = marks[symbol] * (ONE + cost)
                    if marks[symbol] <= stop:
                        skipped.append(
                            SkippedSymbolDay(
                                symbol=symbol,
                                session_date=session.session_date,
                                reason="ENTRY_AT_OR_BELOW_STOP",
                                expected_bars=len(times),
                                observed_bars=len(times),
                            )
                        )
                        continue
                    current_equity = equity()
                    deployed = sum((p.quantity * marks[s] for s, p in positions.items()), ZERO)
                    budget = min(
                        cash,
                        current_equity * ALLOCATION,
                        max(ZERO, current_equity * TOTAL_ALLOCATION - deployed),
                    )
                    stop_risk = entry_price - stop * (ONE - cost)
                    quantity = min(budget / entry_price, current_equity * RISK / stop_risk)
                    quantity = quantity.quantize(QUANTUM, rounding=ROUND_DOWN)
                    if quantity <= ZERO:
                        skipped.append(
                            SkippedSymbolDay(
                                symbol=symbol,
                                session_date=session.session_date,
                                reason="SIZE_BELOW_PRECISION",
                                expected_bars=len(times),
                                observed_bars=len(times),
                            )
                        )
                        continue
                    cash -= entry_price * quantity
                    positions[symbol] = _Position(at, entry_price, quantity, stop)
            drawdown(equity())

            # Only now observe each completed interval and realize intrabar stop proceeds.
            for symbol, position in list(positions.items()):
                if complete[symbol][index].low <= position.stop:
                    close(symbol, position.stop, at + STEP, "STOP")
            marks = {symbol: values[index].close for symbol, values in complete.items()}
            drawdown(equity())

        if positions:
            raise AssertionError("intraday simulation left an overnight position")
        session_results.append(
            IntradaySessionResult(
                session_date=session.session_date,
                starting_equity=starting_equity,
                equity=cash,
                net_return=(cash - starting_equity) / starting_equity,
                included_symbols=tuple(complete),
                excluded_symbols=tuple(symbol for symbol in UNIVERSE if symbol not in complete),
            )
        )
    return IntradayResult(
        input_hash=input_hash,
        starting_cash=starting_cash,
        slippage_bps=slippage_bps,
        trades=tuple(trades),
        sessions=tuple(session_results),
        skipped_symbol_days=tuple(skipped),
        stats=IntradayStats(
            net_return=(cash - starting_cash) / starting_cash,
            max_drawdown=max_drawdown,
            trade_count=len(trades),
            winning_trades=sum(t.net_pnl > ZERO for t in trades),
        ),
    )
