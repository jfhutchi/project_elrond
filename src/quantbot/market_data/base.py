"""Provider-neutral market-data contracts and immutable values."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quantbot.domain import Bar, TradingCalendar


class MarketDataError(RuntimeError):
    """Base error for fail-closed market-data operations."""


class MarketDataRequestError(MarketDataError):
    """Raised when a provider request cannot be completed safely."""


class MarketDataProtocolError(MarketDataError):
    """Raised when a provider response violates the expected protocol."""


class MarketDataTransportError(MarketDataError):
    """Raised by a transport adapter for a retryable connectivity failure."""


class MarketDataFeed(StrEnum):
    IEX = "iex"
    SIP = "sip"
    DELAYED_SIP = "delayed_sip"
    BOATS = "boats"
    OVERNIGHT = "overnight"
    OTC = "otc"


class AdjustmentMode(StrEnum):
    RAW = "raw"
    SPLIT = "split"
    DIVIDEND = "dividend"
    SPIN_OFF = "spin-off"
    ALL = "all"


class MarketDataModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def normalize_datetime(cls, value: object) -> object:
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)


class BarQuery(MarketDataModel):
    symbols: tuple[str, ...]
    start: datetime
    end: datetime
    timeframe: str
    feed: MarketDataFeed
    adjustment: AdjustmentMode
    limit: int = Field(ge=1, le=10000)

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("symbols must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("symbols must be unique")
        for symbol in value:
            if not symbol or symbol != symbol.strip() or symbol != symbol.upper():
                raise ValueError("symbols must be uppercase without surrounding whitespace")
        return value

    @field_validator("timeframe")
    @classmethod
    def validate_timeframe(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("timeframe must not be blank or padded")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.end < self.start:
            raise ValueError("end must be on or after start")
        return self


class MarketDataBatch(MarketDataModel):
    provider: str
    feed: MarketDataFeed
    adjustment: AdjustmentMode
    timeframe: str
    requested_symbols: tuple[str, ...]
    start: datetime
    end: datetime
    bars: tuple[Bar, ...]
    page_count: int = Field(ge=1)

    @field_validator("timeframe")
    @classmethod
    def validate_timeframe(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("timeframe must not be blank or padded")
        return value

    @field_validator("requested_symbols")
    @classmethod
    def validate_requested_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("requested_symbols must not be empty")
        if len(value) != len(set(value)):
            raise ValueError("requested_symbols must be unique")
        if any(
            not symbol or symbol != symbol.strip() or symbol != symbol.upper() for symbol in value
        ):
            raise ValueError("requested_symbols must be uppercase without surrounding whitespace")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.end < self.start:
            raise ValueError("end must be on or after start")
        return self


class MarketSessionClose(MarketDataModel):
    calendar: TradingCalendar
    session_date: date
    close_at: datetime

    @model_validator(mode="after")
    def validate_close(self) -> Self:
        if self.close_at.date() != self.session_date:
            raise ValueError("XNYS close_at must match session_date")
        return self


class TransportResponse(MarketDataModel):
    status_code: int = Field(ge=100, le=599)
    headers: Mapping[str, str]
    content: bytes


class AsyncMarketDataTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        timeout_seconds: Decimal,
    ) -> TransportResponse: ...


Sleeper = Callable[[Decimal], Awaitable[None]]


class HistoricalMarketDataClient(Protocol):
    async def get_historical_bars(self, query: BarQuery) -> MarketDataBatch: ...

    async def get_latest_completed_daily_bars(
        self,
        query: BarQuery,
        *,
        completed_through: datetime,
        sessions: Sequence[MarketSessionClose],
    ) -> Sequence[Bar]: ...
