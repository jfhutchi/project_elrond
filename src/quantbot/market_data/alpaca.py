"""Alpaca historical stock-bar client with bounded retries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import cast

from pydantic import SecretStr

from quantbot.domain import Bar
from quantbot.market_data.base import (
    AsyncMarketDataTransport,
    BarQuery,
    MarketDataBatch,
    MarketDataProtocolError,
    MarketDataRequestError,
    MarketDataTransportError,
    MarketSessionClose,
    Sleeper,
    TransportResponse,
)
from quantbot.market_data.validation import align_daily_bars_to_session_closes

ALPACA_STOCK_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"


async def _default_sleeper(delay: Decimal) -> None:
    await asyncio.sleep(float(delay))


def _utc_parameter(value: datetime) -> str:
    normalized = value.astimezone(UTC).isoformat(timespec="seconds")
    return normalized.replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise MarketDataProtocolError("bar timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MarketDataProtocolError("bar timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MarketDataProtocolError("bar timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _parse_decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise MarketDataProtocolError(f"bar {field} must be numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise MarketDataProtocolError(f"bar {field} must be numeric") from error
    if not parsed.is_finite():
        raise MarketDataProtocolError(f"bar {field} must be finite")
    return parsed


def _decode_payload(response: TransportResponse) -> Mapping[str, object]:
    try:
        loaded = json.loads(
            response.content.decode("utf-8"),
            parse_float=Decimal,
            parse_int=Decimal,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MarketDataProtocolError("provider response is not valid JSON") from error
    if not isinstance(loaded, dict):
        raise MarketDataProtocolError("provider response must be a JSON object")
    return cast(Mapping[str, object], loaded)


def _parse_bars(payload: Mapping[str, object]) -> list[Bar]:
    raw_by_symbol = payload.get("bars")
    if not isinstance(raw_by_symbol, dict):
        raise MarketDataProtocolError("provider response bars must be an object")
    observations: list[Bar] = []
    for symbol, raw_bars in raw_by_symbol.items():
        if not isinstance(symbol, str) or not isinstance(raw_bars, list):
            raise MarketDataProtocolError("provider response contains malformed symbol bars")
        for raw_bar in raw_bars:
            if not isinstance(raw_bar, dict):
                raise MarketDataProtocolError("provider bar must be an object")
            observations.append(
                Bar(
                    symbol=symbol,
                    timestamp=_parse_timestamp(raw_bar.get("t")),
                    open=_parse_decimal(raw_bar.get("o"), "open"),
                    high=_parse_decimal(raw_bar.get("h"), "high"),
                    low=_parse_decimal(raw_bar.get("l"), "low"),
                    close=_parse_decimal(raw_bar.get("c"), "close"),
                    volume=_parse_decimal(raw_bar.get("v"), "volume"),
                    adjustment=Decimal("1"),
                )
            )
    return observations


class AlpacaMarketDataClient:
    """Fetch split/dividend-aware stock bars without exposing credentials."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        api_secret: SecretStr,
        transport: AsyncMarketDataTransport,
        sleeper: Sleeper | None = None,
        max_retries: int = 2,
        timeout_seconds: Decimal = Decimal("10"),
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be nonnegative")
        if not timeout_seconds.is_finite() or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        if not api_key.get_secret_value() or not api_secret.get_secret_value():
            raise ValueError("Alpaca credentials must not be empty")
        self._api_key = api_key
        self._api_secret = api_secret
        self._transport = transport
        self._sleeper = sleeper or _default_sleeper
        self._max_retries = max_retries
        self._timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return (
            "AlpacaMarketDataClient(api_key=SecretStr('**********'), "
            "api_secret=SecretStr('**********'))"
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._api_key.get_secret_value(),
            "APCA-API-SECRET-KEY": self._api_secret.get_secret_value(),
            "Accept": "application/json",
        }

    async def _request_page(self, params: Mapping[str, str]) -> TransportResponse:
        attempt = 0
        while True:
            try:
                response = await self._transport.request(
                    "GET",
                    ALPACA_STOCK_BARS_URL,
                    headers=self._headers,
                    params=params,
                    timeout_seconds=self._timeout_seconds,
                )
            except MarketDataTransportError as error:
                if attempt >= self._max_retries:
                    raise MarketDataRequestError(
                        "Alpaca market-data transport failed after bounded retries"
                    ) from error
                await self._sleeper(Decimal(2) ** attempt)
                attempt += 1
                continue
            if response.status_code == 429 and attempt < self._max_retries:
                delay = self._retry_after(response.headers)
                await self._sleeper(delay)
                attempt += 1
                continue
            if 500 <= response.status_code < 600 and attempt < self._max_retries:
                await self._sleeper(Decimal(2) ** attempt)
                attempt += 1
                continue
            if not 200 <= response.status_code < 300:
                raise MarketDataRequestError(
                    f"Alpaca market-data request failed with HTTP {response.status_code}"
                )
            return response

    @staticmethod
    def _retry_after(headers: Mapping[str, str]) -> Decimal:
        raw = next(
            (value for key, value in headers.items() if key.lower() == "retry-after"),
            "1",
        )
        try:
            delay = Decimal(raw)
        except InvalidOperation as error:
            raise MarketDataProtocolError("Retry-After header is invalid") from error
        if not delay.is_finite() or delay < 0:
            raise MarketDataProtocolError("Retry-After header is invalid")
        return delay

    async def get_historical_bars(self, query: BarQuery) -> MarketDataBatch:
        base_params = {
            "symbols": ",".join(sorted(query.symbols)),
            "timeframe": query.timeframe,
            "start": _utc_parameter(query.start),
            "end": _utc_parameter(query.end),
            "limit": str(query.limit),
            "adjustment": query.adjustment.value,
            "feed": query.feed.value,
            "sort": "asc",
        }
        page_token: str | None = None
        seen_tokens: set[str] = set()
        bars: list[Bar] = []
        page_count = 0
        while True:
            params = dict(base_params)
            if page_token is not None:
                params["page_token"] = page_token
            response = await self._request_page(params)
            payload = _decode_payload(response)
            bars.extend(_parse_bars(payload))
            page_count += 1
            raw_token = payload.get("next_page_token")
            if raw_token is None:
                break
            if not isinstance(raw_token, str) or not raw_token:
                raise MarketDataProtocolError("next_page_token must be a nonempty string or null")
            if raw_token in seen_tokens:
                raise MarketDataProtocolError("pagination token cycle detected")
            seen_tokens.add(raw_token)
            page_token = raw_token

        return MarketDataBatch(
            provider="alpaca",
            feed=query.feed,
            adjustment=query.adjustment,
            timeframe=query.timeframe,
            requested_symbols=tuple(sorted(query.symbols)),
            start=query.start,
            end=query.end,
            bars=tuple(sorted(bars, key=lambda bar: (bar.timestamp, bar.symbol))),
            page_count=page_count,
        )

    async def get_latest_completed_daily_bars(
        self,
        query: BarQuery,
        *,
        completed_through: datetime,
        sessions: tuple[MarketSessionClose, ...],
    ) -> tuple[Bar, ...]:
        if completed_through.tzinfo is None or completed_through.utcoffset() is None:
            raise ValueError("completed_through must be timezone-aware")
        cutoff = completed_through.astimezone(UTC)
        batch = align_daily_bars_to_session_closes(
            await self.get_historical_bars(query),
            sessions=sessions,
        )
        latest: dict[str, Bar] = {}
        for bar in batch.bars:
            if bar.timestamp <= cutoff:
                latest[bar.symbol] = bar
        missing = sorted(set(query.symbols) - set(latest))
        if missing:
            raise MarketDataProtocolError(
                f"no completed daily bar for requested symbols: {', '.join(missing)}"
            )
        return tuple(latest[symbol] for symbol in sorted(latest))
