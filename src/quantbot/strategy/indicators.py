"""Pure Decimal indicators over pre-adjusted, ordered bar series."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal

from quantbot.domain import Bar


class IndicatorInputError(ValueError):
    """Raised when an indicator receives an unordered or mixed-symbol series."""


def _validate_bars(bars: Sequence[Bar]) -> None:
    if not bars:
        return
    symbol = bars[0].symbol
    previous = bars[0].timestamp
    if previous.tzinfo is None or previous.utcoffset() is None:
        raise IndicatorInputError("bar timestamps must be timezone-aware")
    for bar in bars[1:]:
        if bar.symbol != symbol:
            raise IndicatorInputError("all bars must have the same symbol")
        if bar.timestamp.tzinfo is None or bar.timestamp.utcoffset() is None:
            raise IndicatorInputError("bar timestamps must be timezone-aware")
        if bar.timestamp <= previous:
            raise IndicatorInputError("bars must be strictly increasing without duplicates")
        previous = bar.timestamp


def _validate_period(period: int) -> None:
    if period <= 0:
        raise ValueError("period must be positive")


def sma(
    bars: Sequence[Bar],
    period: int,
    value: Callable[[Bar], Decimal] = lambda bar: bar.close,
) -> Decimal | None:
    """Return the current simple average; providers supply adjusted Bar series."""
    _validate_period(period)
    _validate_bars(bars)
    if len(bars) < period:
        return None
    return sum((value(bar) for bar in bars[-period:]), Decimal(0)) / Decimal(period)


def momentum_12_1(
    bars: Sequence[Bar],
    long_period: int = 252,
    skip: int = 21,
) -> Decimal | None:
    """Return long-horizon momentum with the most recent ``skip`` bars omitted."""
    _validate_bars(bars)
    _validate_period(long_period)
    if skip < 0 or skip >= long_period:
        raise ValueError("skip must be nonnegative and less than long_period")
    if len(bars) < long_period + 1:
        return None
    return bars[-(skip + 1)].close / bars[-(long_period + 1)].close - Decimal(1)


def donchian_entry_level(bars: Sequence[Bar], period: int = 55) -> Decimal | None:
    """Return the prior-window high, excluding the current bar."""
    _validate_period(period)
    _validate_bars(bars)
    if len(bars) < period + 1:
        return None
    return max(bar.high for bar in bars[-(period + 1) : -1])


def donchian_exit_level(bars: Sequence[Bar], period: int = 20) -> Decimal | None:
    """Return the prior-window low, excluding the current bar."""
    _validate_period(period)
    _validate_bars(bars)
    if len(bars) < period + 1:
        return None
    return min(bar.low for bar in bars[-(period + 1) : -1])


#: Sessions in a trading year, for annualising a daily volatility estimate.
TRADING_DAYS_PER_YEAR = 252


def true_ranges(bars: Sequence[Bar]) -> tuple[Decimal, ...]:
    """Return Wilder true ranges for every supplied bar."""
    _validate_bars(bars)
    if not bars:
        return ()
    ranges = [bars[0].high - bars[0].low]
    for previous, current in zip(bars, bars[1:], strict=False):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return tuple(ranges)


def realized_volatility(bars: Sequence[Bar], period: int = 20) -> Decimal | None:
    """Annualised standard deviation of daily close-to-close returns, as a fraction.

    Deliberately a different measure from ATR. ATR sizes the stop, so it answers "how far can
    this move against me before I am wrong". This answers "how turbulent is this right now",
    which is what exposure should scale against. Uses the population standard deviation so the
    result is a deterministic function of the window with no small-sample correction to argue
    about, and returns None rather than guessing when the window is not full.
    """
    _validate_period(period)
    if len(bars) < period + 1:
        return None
    window = bars[-(period + 1):]
    returns: list[Decimal] = []
    for previous, current in zip(window, window[1:], strict=False):
        if previous.close <= 0:
            return None
        returns.append(current.close / previous.close - Decimal(1))
    mean = sum(returns, Decimal(0)) / Decimal(len(returns))
    variance = sum(((r - mean) ** 2 for r in returns), Decimal(0)) / Decimal(len(returns))
    if variance <= 0:
        return Decimal(0)
    return variance.sqrt() * Decimal(TRADING_DAYS_PER_YEAR).sqrt()


def wilder_atr(bars: Sequence[Bar], period: int = 20) -> Decimal | None:
    """Return Wilder ATR seeded with the first arithmetic mean."""
    _validate_period(period)
    ranges = true_ranges(bars)
    if len(ranges) < period:
        return None
    previous = sum(ranges[:period], Decimal(0)) / Decimal(period)
    for current in ranges[period:]:
        previous = (previous * Decimal(period - 1) + current) / Decimal(period)
    return previous


def highest_high_since(bars: Sequence[Bar], entered_at: datetime) -> Decimal | None:
    """Return the inclusive high-water mark since an aware entry timestamp."""
    if entered_at.tzinfo is None or entered_at.utcoffset() is None:
        raise ValueError("entered_at must be timezone-aware")
    _validate_bars(bars)
    highs = [bar.high for bar in bars if bar.timestamp >= entered_at]
    return max(highs) if highs else None
