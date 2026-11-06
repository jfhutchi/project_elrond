"""Explicit spot-PAPER amendment and shared Eastern entry-risk windows."""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from quantbot.brokers.base import BrokerModel

_EASTERN = ZoneInfo("America/New_York")


def _aware_utc(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("risk and market windows require aware time")
    return at.astimezone(UTC)


def sleep_window(at: datetime) -> tuple[datetime, datetime] | None:
    """Return the active 22:00–06:00 Eastern interval as UTC bounds, or None."""
    local = _aware_utc(at).astimezone(_EASTERN)
    if 6 <= local.hour < 22:
        return None
    start_day = local.date() - timedelta(days=int(local.hour < 6))
    return (
        datetime.combine(start_day, time(22), tzinfo=_EASTERN).astimezone(UTC),
        datetime.combine(start_day + timedelta(days=1), time(6), tzinfo=_EASTERN).astimezone(UTC),
    )


def in_sleep_window(at: datetime) -> bool:
    """The sleep interval is start-inclusive and end-exclusive, including DST folds."""
    return sleep_window(at) is not None


def account_risk_day_bounds(at: datetime) -> tuple[datetime, datetime]:
    """UTC bounds for the containing Eastern account day; DST days have 23/25 hours."""
    day = _aware_utc(at).astimezone(_EASTERN).date()
    return (
        datetime.combine(day, time(), tzinfo=_EASTERN).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), time(), tzinfo=_EASTERN).astimezone(UTC),
    )


def crypto_session_bounds(at: datetime) -> tuple[datetime, datetime]:
    """Crypto market observations belong to their containing UTC day, even at midnight."""
    start = datetime.combine(_aware_utc(at).date(), time(), tzinfo=UTC)
    return start, start + timedelta(days=1)


def _usd_symbol(symbol: str) -> bool:
    # Syntax is not evidence of broker eligibility; acquisition and send verify that separately.
    return re.fullmatch(r"[A-Z0-9]{2,12}/USD", symbol) is not None and symbol != "USD/USD"


class CryptoPaperPolicy(BrokerModel):
    """Fixed project defaults; the modeled 25 bps applies per fill on each side."""

    contract: Literal["ALPACA_SPOT_PAPER_V1"] = "ALPACA_SPOT_PAPER_V1"
    symbols: tuple[str, ...] = ("BTC/USD", "ETH/USD", "SOL/USD")
    estimated_taker_fee_bps: Literal[25] = 25
    fee_estimate_source: Literal["PROJECT_DEFAULT"] = "PROJECT_DEFAULT"
    sleep_timezone: Literal["America/New_York"] = "America/New_York"
    sleep_start: Literal["22:00"] = "22:00"
    sleep_end: Literal["06:00"] = "06:00"

    @model_validator(mode="after")
    def bounded_spot_universe(self) -> Self:
        if (
            not 1 <= len(self.symbols) <= 3
            or len(set(self.symbols)) != len(self.symbols)
            or not all(_usd_symbol(symbol) for symbol in self.symbols)
        ):
            raise ValueError("one to three unique slash-delimited USD crypto symbols required")
        return self


class CryptoMarketSession(BrokerModel):
    """Operational session bound to broker asset evidence, never an offline capability."""

    symbol: str
    benchmark: Literal["BTC/USD"] = "BTC/USD"
    session_open: datetime
    session_close: datetime
    observed_at: datetime
    asset_eligibility_id: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def exact_utc_day(self) -> Self:
        if not _usd_symbol(self.symbol):
            raise ValueError("slash-delimited USD crypto symbol required")
        if (self.session_open, self.session_close) != crypto_session_bounds(self.observed_at):
            raise ValueError("crypto session requires exact UTC day boundaries")
        return self
