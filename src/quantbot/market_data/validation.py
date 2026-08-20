"""Fail-closed validation for completed market-data batches."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import model_validator

from quantbot.domain import Bar
from quantbot.market_data.base import (
    AdjustmentMode,
    MarketDataBatch,
    MarketDataFeed,
    MarketDataModel,
    MarketSessionClose,
)


class MarketDataValidationError(ValueError):
    """Raised when observed bars cannot be used without ambiguity."""


class DataGapReason(StrEnum):
    MISSING_EXPECTED_BAR = "MISSING_EXPECTED_BAR"
    IMPLAUSIBLE_RANGE = "IMPLAUSIBLE_RANGE"


# A corrupt extreme is one the open and close do not corroborate. A real collapse drags the close
# down with it; a dropped digit leaves the close where it was and only the low moves. So the test
# is the extreme against the open/close bracket rather than any absolute range, which is what makes
# it safe for genuinely volatile single names.
#
# Calibrated 2026-08-19 against the full 96,370-bar research history: exactly one bar trips it --
# SPY 2026-02-02, O=685.90 H=693.21 L=68.64 C=691.70, a dropped digit -- at a low/min(open,close)
# ratio of 0.1001. The lowest ratio among all other bars is 0.8927 (XLE 2020-11-09). The threshold
# sits in an empty margin nearly 0.8 wide, so it is not a tuned parameter.
#
# That bar cost 20.1% of equity in a single session in a SPY-only backtest: it tripped a stop at a
# price no trade ever occurred at, while SPY actually rose that day. Bars pass `Bar.validate_ohlc`
# because that check is ordinal -- low <= min(open, high, close) -- and 68.64 satisfies it.
IMPLAUSIBLE_LOW_RATIO = Decimal("0.5")
IMPLAUSIBLE_HIGH_RATIO = Decimal("2")


def implausible_range(bar: Bar) -> bool:
    """True when a bar's extreme is uncorroborated by its own open and close."""
    return (
        bar.low < IMPLAUSIBLE_LOW_RATIO * min(bar.open, bar.close)
        or bar.high > IMPLAUSIBLE_HIGH_RATIO * max(bar.open, bar.close)
    )


class DataGap(MarketDataModel):
    symbol: str
    timestamp: datetime
    reason: DataGapReason


class BarValidationReport(MarketDataModel):
    provider: str
    feed: MarketDataFeed
    adjustment: AdjustmentMode
    timeframe: str
    requested_symbols: tuple[str, ...]
    valid_bars: tuple[Bar, ...]
    gaps: tuple[DataGap, ...]
    eligible_symbols: tuple[str, ...]
    ineligible_symbols: tuple[str, ...]
    stale_symbols: tuple[str, ...]
    completed_through: datetime

    @model_validator(mode="after")
    def validate_identity_sets(self) -> Self:
        requested = set(self.requested_symbols)
        if not requested or len(requested) != len(self.requested_symbols):
            raise ValueError("requested_symbols must be nonempty and unique")
        eligible = set(self.eligible_symbols)
        ineligible = set(self.ineligible_symbols)
        partition_is_invalid = eligible & ineligible or eligible | ineligible != requested
        if partition_is_invalid:
            raise ValueError("eligible and ineligible symbols must partition requested_symbols")
        if not set(self.stale_symbols) <= ineligible:
            raise ValueError("stale_symbols must be ineligible")
        if any(gap.symbol not in ineligible for gap in self.gaps):
            raise ValueError("gap symbols must be ineligible")
        if any(bar.symbol not in requested for bar in self.valid_bars):
            raise ValueError("valid bar symbols must be requested")
        return self


def _normalize_expected(expected_timestamps: tuple[datetime, ...]) -> tuple[datetime, ...]:
    if not expected_timestamps:
        raise ValueError("expected timestamps must not be empty")
    normalized: list[datetime] = []
    for value in expected_timestamps:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expected timestamps must be timezone-aware")
        normalized.append(value.astimezone(UTC))
    if any(
        current <= previous for previous, current in zip(normalized, normalized[1:], strict=False)
    ):
        raise ValueError("expected timestamps must be strictly increasing")
    return tuple(normalized)


def align_daily_bars_to_session_closes(
    batch: MarketDataBatch,
    *,
    sessions: tuple[MarketSessionClose, ...],
) -> MarketDataBatch:
    """Replace Alpaca daily labels with authoritative completed XNYS close times."""
    if batch.timeframe != "1Day":
        raise MarketDataValidationError("session-close alignment requires 1Day bars")
    by_date: dict[date, MarketSessionClose] = {}
    for session in sessions:
        if session.session_date in by_date:
            raise MarketDataValidationError(
                f"duplicate XNYS session date: {session.session_date.isoformat()}"
            )
        by_date[session.session_date] = session

    aligned: list[Bar] = []
    for bar in batch.bars:
        mapped_session = by_date.get(bar.timestamp.date())
        if mapped_session is None:
            raise MarketDataValidationError(
                f"missing XNYS session for daily bar date {bar.timestamp.date().isoformat()}"
            )
        aligned.append(bar.model_copy(update={"timestamp": mapped_session.close_at}))
    return batch.model_copy(
        update={"bars": tuple(sorted(aligned, key=lambda bar: (bar.timestamp, bar.symbol)))}
    )


def validate_bar_batch(
    batch: MarketDataBatch,
    *,
    expected_timestamps: tuple[datetime, ...],
    completed_through: datetime,
) -> BarValidationReport:
    """Validate exact completed sessions and mark gaps without synthesizing bars."""
    if completed_through.tzinfo is None or completed_through.utcoffset() is None:
        raise ValueError("completed_through must be timezone-aware")
    cutoff = completed_through.astimezone(UTC)
    expected = _normalize_expected(expected_timestamps)
    expected_set = set(expected)
    requested_set = set(batch.requested_symbols)
    seen: set[tuple[str, datetime]] = set()
    observed_by_symbol: dict[str, set[datetime]] = {
        symbol: set() for symbol in batch.requested_symbols
    }
    previous_by_symbol: dict[str, datetime] = {}
    implausible: dict[str, list[datetime]] = {}

    for bar in batch.bars:
        if bar.symbol not in requested_set:
            raise MarketDataValidationError(f"unexpected symbol in batch: {bar.symbol}")
        if bar.timestamp > cutoff:
            raise MarketDataValidationError(
                f"future bar for {bar.symbol} at {bar.timestamp.isoformat()}"
            )
        if bar.timestamp not in expected_set:
            raise MarketDataValidationError(
                f"unexpected timestamp for {bar.symbol}: {bar.timestamp.isoformat()}"
            )
        key = (bar.symbol, bar.timestamp)
        if key in seen:
            raise MarketDataValidationError(
                f"duplicate bar for {bar.symbol} at {bar.timestamp.isoformat()}"
            )
        previous = previous_by_symbol.get(bar.symbol)
        if previous is not None and bar.timestamp <= previous:
            raise MarketDataValidationError(f"bars for {bar.symbol} must be strictly increasing")
        seen.add(key)
        # Counted as observed so it does not also raise a MISSING_EXPECTED_BAR for the same
        # session; it is reported under its own reason and still makes the symbol ineligible.
        observed_by_symbol[bar.symbol].add(bar.timestamp)
        previous_by_symbol[bar.symbol] = bar.timestamp
        if implausible_range(bar):
            implausible.setdefault(bar.symbol, []).append(bar.timestamp)

    gaps: list[DataGap] = []
    eligible: list[str] = []
    ineligible: list[str] = []
    stale: list[str] = []
    latest_expected = expected[-1] if expected else None
    for symbol in sorted(batch.requested_symbols):
        missing = [value for value in expected if value not in observed_by_symbol[symbol]]
        corrupt = sorted(implausible.get(symbol, ()))
        gaps.extend(
            DataGap(symbol=symbol, timestamp=value, reason=DataGapReason.IMPLAUSIBLE_RANGE)
            for value in corrupt
        )
        if missing or corrupt:
            ineligible.append(symbol)
            if latest_expected is not None and latest_expected in missing:
                stale.append(symbol)
            gaps.extend(
                DataGap(
                    symbol=symbol,
                    timestamp=value,
                    reason=DataGapReason.MISSING_EXPECTED_BAR,
                )
                for value in missing
            )
        elif not corrupt:
            eligible.append(symbol)

    return BarValidationReport(
        provider=batch.provider,
        feed=batch.feed,
        adjustment=batch.adjustment,
        timeframe=batch.timeframe,
        requested_symbols=batch.requested_symbols,
        valid_bars=tuple(bar for bar in batch.bars if not implausible_range(bar)),
        gaps=tuple(gaps),
        eligible_symbols=tuple(eligible),
        ineligible_symbols=tuple(ineligible),
        stale_symbols=tuple(stale),
        completed_through=cutoff,
    )
