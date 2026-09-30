"""Read-only Alpaca captures; receipt evidence is not shadow-trading authority.

Callers enforce research-window access before loading captures or computing features.
Historical bars fetched today become available today, never retroactively at their close.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from quantbot.config import Settings
from quantbot.learner.data import DailyInput
from quantbot.market_data.alpaca import ALPACA_STOCK_BARS_URL, AlpacaMarketDataClient
from quantbot.market_data.base import (
    AdjustmentMode,
    AsyncMarketDataTransport,
    BarQuery,
    MarketDataFeed,
    MarketDataModel,
    MarketDataProtocolError,
    TransportResponse,
)
from quantbot.market_data.calendar import (
    ALPACA_PAPER_CALENDAR_URL,
    AlpacaMarketCalendar,
    session_closes,
    session_sequence,
)
from quantbot.market_data.transports import HttpxMarketDataTransport
from quantbot.market_data.validation import align_daily_bars_to_session_closes
from quantbot.strategy import XNYSSessionSequence

SOURCE_ID: Literal['alpaca:iex:all:1day'] = 'alpaca:iex:all:1day'
INTRADAY_SOURCE_ID: Literal['alpaca:iex:raw:1min'] = 'alpaca:iex:raw:1min'
MAX_CAPTURE_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REQUESTS = 64


def _encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('capture clock must be timezone-aware')
    return value.astimezone(UTC)


class Receipt(MarketDataModel):
    url: Literal[
        'https://data.alpaca.markets/v2/stocks/bars',
        'https://paper-api.alpaca.markets/v2/calendar',
    ]
    query_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    response_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    received_at: datetime
    status_code: int = Field(ge=100, le=599)
    response_bytes: int = Field(ge=0, le=MAX_RESPONSE_BYTES)


class Snapshot(MarketDataModel):
    schema_version: Literal[1] = 1
    source_id: Literal['alpaca:iex:all:1day', 'alpaca:iex:raw:1min'] = SOURCE_ID
    sessions: XNYSSessionSequence
    # DailyInput is the existing immutable bar/availability container. For 1Min captures,
    # bar timestamps describe interval starts and are never mapped to daily closes.
    inputs: tuple[DailyInput, ...] = Field(max_length=250000)
    received_at: datetime
    query: BarQuery
    receipts: tuple[Receipt, ...] = Field(min_length=1, max_length=MAX_REQUESTS)
    empty_reason: Literal['OUTSIDE_FORECAST_SESSION', 'INCOMPLETE_DATA'] | None = None

    @property
    def query_hash(self) -> str:
        return _hash(_encoded(self.query.model_dump(mode='json')))

    @property
    def identity(self) -> str:
        return _hash(_encoded(self.model_dump(mode='json', exclude_none=True)))

    @model_validator(mode='after')
    def validate_capture(self) -> Self:
        daily = self.query.timeframe == '1Day'
        expected_adjustment = AdjustmentMode.ALL if daily else AdjustmentMode.RAW
        expected_source = SOURCE_ID if daily else INTRADAY_SOURCE_ID
        if (self.query.timeframe not in ('1Day', '1Min')
                or self.query.feed is not MarketDataFeed.IEX
                or self.query.adjustment is not expected_adjustment
                or self.source_id != expected_source):
            raise ValueError('capture timeframe, adjustment and source must match fixed IEX feed')
        if self.sessions.calendar != 'XNYS' or 'SPY' not in self.query.symbols:
            raise ValueError('capture requires XNYS sessions and SPY benchmark')
        if self.received_at != max(receipt.received_at for receipt in self.receipts):
            raise ValueError('capture receipt timestamp must match last receipt')
        if not self.inputs:
            if daily or self.empty_reason is None:
                raise ValueError('empty minute capture requires an explicit reason')
        elif self.empty_reason is not None:
            raise ValueError('nonempty capture cannot have an empty reason')
        if len(self.receipts) < 2:
            if (self.inputs or self.empty_reason != 'OUTSIDE_FORECAST_SESSION'
                    or self.receipts[0].url != ALPACA_PAPER_CALENDAR_URL
                    or any(self.query.start <= session.open_at <= self.query.end
                           for session in self.sessions.sessions)):
                raise ValueError('calendar-only capture requires no eligible session minute')
        closes = {session.close_at for session in self.sessions.sessions}
        seen: set[tuple[str, datetime]] = set()
        for item in self.inputs:
            key = item.bar.symbol, item.bar.timestamp
            if key in seen:
                raise ValueError('duplicate capture bar')
            seen.add(key)
            in_calendar = (item.bar.timestamp in closes if daily
                           else _regular_minute(item.bar.timestamp, self.sessions))
            if (item.bar.symbol not in self.query.symbols or not in_calendar
                    or not self.query.start <= item.bar.timestamp <= self.query.end):
                raise ValueError('capture bar is outside query or calendar')
            if item.available_at != self.received_at:
                raise ValueError('capture availability must use actual receipt time')
            if not daily and item.available_at <= item.bar.timestamp + timedelta(minutes=1):
                raise ValueError('minute capture must arrive after its interval end')
        if self.inputs and {item.bar.symbol for item in self.inputs} != set(self.query.symbols):
            raise ValueError('capture is missing requested symbols')
        return self


def _regular_minute(timestamp: datetime, sessions: XNYSSessionSequence) -> bool:
    if timestamp.second or timestamp.microsecond:
        return False
    return any(session.open_at <= timestamp
               and timestamp + timedelta(minutes=1) <= session.close_at
               for session in sessions.sessions)


class _RecordingTransport:
    def __init__(self, transport: AsyncMarketDataTransport, clock: Callable[[], datetime]) -> None:
        self.transport = transport
        self.clock = clock
        self.receipts: list[Receipt] = []
        self.requests = 0
        self.total_bytes = 0

    async def request(
        self, method: str, url: str, *, headers: Mapping[str, str],
        params: Mapping[str, str], timeout_seconds: Decimal,
    ) -> TransportResponse:
        if method != 'GET' or url not in (ALPACA_STOCK_BARS_URL, ALPACA_PAPER_CALENDAR_URL):
            raise MarketDataProtocolError('capture only permits official read-only endpoints')
        self.requests += 1
        if self.requests > MAX_REQUESTS:
            raise MarketDataProtocolError('capture request limit exceeded')
        response = await self.transport.request(
            method, url, headers=headers, params=params, timeout_seconds=timeout_seconds,
        )
        received_at = _utc(self.clock)
        size = len(response.content)
        self.total_bytes += size
        if size > MAX_RESPONSE_BYTES or self.total_bytes > MAX_CAPTURE_BYTES:
            raise MarketDataProtocolError('capture response size limit exceeded')
        self.receipts.append(Receipt(
            url=url,
            query_hash=_hash(_encoded(dict(params))),
            response_hash=_hash(response.content), received_at=received_at,
            status_code=response.status_code, response_bytes=size,
        ))
        return response


def _archive(root: Path, snapshot: Snapshot) -> None:
    content = _encoded(snapshot.model_dump(mode='json', exclude_none=True))
    if len(content) > MAX_CAPTURE_BYTES:
        raise ValueError('snapshot size limit exceeded')
    directory = root / 'market' / 'snapshots'
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f'{snapshot.identity}.json'
    descriptor, filename = tempfile.mkstemp(prefix='.capture-', dir=directory)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Hard-link publication is atomic and cannot replace an existing capture.
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.stat().st_size != len(content) or destination.read_bytes() != content:
                raise ValueError('existing snapshot hash collision or archive corruption') from None
    finally:
        temporary.unlink(missing_ok=True)


def read_snapshot(path: Path) -> Snapshot:
    """Verify a bounded content-addressed archive after the caller's access guard.

    Integrity confirms the bytes, not independent authenticity of a caller-supplied file.
    """
    if not re.fullmatch(r'[0-9a-f]{64}\.json', path.name):
        raise ValueError('snapshot filename must contain its content hash')
    with path.open('rb') as handle:
        content = handle.read(MAX_CAPTURE_BYTES + 1)
    if len(content) > MAX_CAPTURE_BYTES:
        raise ValueError('snapshot size limit exceeded')
    if _hash(content) != path.stem:
        raise ValueError('snapshot content hash mismatch')
    snapshot = Snapshot.model_validate_json(content)
    if snapshot.identity != path.stem:
        raise ValueError('snapshot canonical hash mismatch')
    return snapshot


async def collect_snapshot(
    root: Path, symbols: tuple[str, ...], start: date, end: date, settings: Settings, *,
    transport: AsyncMarketDataTransport | None = None,
    clock: Callable[[], datetime] | None = None,
    timeframe: Literal['1Day', '1Min'] = '1Day',
) -> Snapshot:
    """Capture completed daily or regular-session minute bars with paper credentials."""
    if timeframe not in ('1Day', '1Min'):
        raise ValueError('capture timeframe must be 1Day or 1Min')
    if end < start or (end - start).days > 3660:
        raise ValueError('capture window must be ordered and at most 3660 days')
    if (not symbols or len(symbols) > 128 or len(set(symbols)) != len(symbols)
            or any(re.fullmatch(r'[A-Z][A-Z0-9.-]{0,14}', symbol) is None for symbol in symbols)):
        raise ValueError('capture symbols must be unique uppercase stock symbols')
    key, secret = settings.ALPACA_PAPER_API_KEY, settings.ALPACA_PAPER_API_SECRET
    if key is None or secret is None:
        raise ValueError('Alpaca paper credentials are required for market capture')
    capture_clock = clock or (lambda: datetime.now(UTC))
    cutoff = _utc(capture_clock)
    owned_transport = HttpxMarketDataTransport() if transport is None else None
    base_transport = owned_transport if owned_transport is not None else transport
    assert base_transport is not None
    recording = _RecordingTransport(base_transport, capture_clock)
    try:
        calendar = AlpacaMarketCalendar(api_key=key, api_secret=secret, transport=recording)
        sessions = await calendar.get_sessions(start, end + timedelta(days=14))
        if any(not start <= session.session_date <= end + timedelta(days=14)
               for session in sessions):
            raise MarketDataProtocolError('calendar returned dates outside capture window')
        sequence = session_sequence(sessions)
        if timeframe == '1Day':
            completed = tuple(session for session in sessions
                              if session.session_date <= end and session.close_at <= cutoff)
            if not completed:
                raise MarketDataProtocolError('capture window has no completed sessions')
            query_end = completed[-1].close_at
        else:
            query_end = min(datetime.combine(end, time.max, tzinfo=UTC),
                            cutoff.replace(second=0, microsecond=0) - timedelta(minutes=1))
        query = BarQuery(
            symbols=tuple(sorted(set(symbols) | {'SPY'})),
            start=datetime.combine(start, time.min, tzinfo=UTC),
            end=max(datetime.combine(start, time.min, tzinfo=UTC), query_end),
            timeframe=timeframe, feed=MarketDataFeed.IEX,
            adjustment=AdjustmentMode.ALL if timeframe == '1Day' else AdjustmentMode.RAW,
            limit=10000,
        )
        if timeframe == '1Min' and not any(
            query.start <= session.open_at <= query.end for session in sessions
        ):
            received_at = max(receipt.received_at for receipt in recording.receipts)
            if received_at < cutoff:
                raise MarketDataProtocolError('capture clock moved backwards')
            snapshot = Snapshot(
                source_id=INTRADAY_SOURCE_ID, sessions=sequence, inputs=(),
                received_at=received_at, query=query, receipts=tuple(recording.receipts),
                empty_reason='OUTSIDE_FORECAST_SESSION',
            )
            _archive(root, snapshot)
            return snapshot
        client = AlpacaMarketDataClient(api_key=key, api_secret=secret, transport=recording)
        batch = await client.get_historical_bars(query)
        received_at = max(receipt.received_at for receipt in recording.receipts)
        if received_at < cutoff:
            raise MarketDataProtocolError('capture clock moved backwards')
        if timeframe == '1Day':
            batch = align_daily_bars_to_session_closes(batch, sessions=session_closes(sessions))
            if any(not query.start <= bar.timestamp <= query.end for bar in batch.bars):
                raise MarketDataProtocolError('provider bar outside completed query window')
            bars = batch.bars
        else:
            bars = tuple(bar for bar in batch.bars
                         if query.start <= bar.timestamp <= query.end
                         and bar.timestamp + timedelta(minutes=1) < received_at
                         and _regular_minute(bar.timestamp, sequence))
        snapshot = Snapshot(
            source_id=SOURCE_ID if timeframe == '1Day' else INTRADAY_SOURCE_ID,
            sessions=sequence, inputs=tuple(DailyInput(bar=bar, available_at=received_at)
                                            for bar in bars), received_at=received_at,
            query=query, receipts=tuple(recording.receipts),
            empty_reason=(None if bars else
                          'INCOMPLETE_DATA' if any(session.open_at <= received_at < session.close_at
                                                   for session in sessions)
                          else 'OUTSIDE_FORECAST_SESSION'),
        )
        _archive(root, snapshot)
        return snapshot
    finally:
        if owned_transport is not None:
            await owned_transport.aclose()
