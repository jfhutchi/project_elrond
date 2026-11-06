"""Alpaca crypto bar client.

Crypto bars live on a different endpoint with a different shape from equities: no feed
selection and no adjustment, because there are no exchanges to consolidate and no corporate
actions to adjust for. That is why this is a sibling client rather than a parameter on
AlpacaMarketDataClient — pretending crypto has a feed and an adjustment mode would put a
lie in the provider key that identifies cached bars.

Everything downstream is reused unchanged: the same MarketDataBatch, the same
validate_bar_batch, the same MarketDataCache, the same session-close alignment.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import cast

from pydantic import SecretStr, ValidationError

from quantbot.domain import Bar
from quantbot.market_data.base import (
    AdjustmentMode,
    AsyncMarketDataTransport,
    BarQuery,
    MarketDataBatch,
    MarketDataFeed,
    MarketDataProtocolError,
    MarketDataRequestError,
    MarketDataTransportError,
    Sleeper,
    TransportResponse,
)

ALPACA_CRYPTO_BARS_URL = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
ALPACA_CRYPTO_QUOTES_URL = "https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes"

#: Crypto has no corporate actions, so bars are unadjusted by nature rather than by choice.
CRYPTO_ADJUSTMENT = AdjustmentMode.RAW


async def _default_sleeper(delay: Decimal) -> None:
    await asyncio.sleep(float(delay))


def _decimal(payload: Mapping[str, object], field: str, symbol: str) -> Decimal:
    value = payload.get(field)
    if value is None or isinstance(value, bool):
        raise MarketDataProtocolError(f"crypto bar {symbol} field {field} must be numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise MarketDataProtocolError(
            f"crypto bar {symbol} field {field} must be numeric"
        ) from error
    if not parsed.is_finite():
        raise MarketDataProtocolError(f"crypto bar {symbol} field {field} must be finite")
    return parsed


def _timestamp(payload: Mapping[str, object], symbol: str) -> datetime:
    value = payload.get("t")
    if not isinstance(value, str):
        raise MarketDataProtocolError(f"crypto bar {symbol} timestamp must be text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MarketDataProtocolError(f"crypto bar {symbol} timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MarketDataProtocolError(f"crypto bar {symbol} timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _bar(
    symbol: str, payload: Mapping[str, object], *, quote_price_bars: bool = False
) -> Bar | None:
    """Preserve provider prices, optionally including explicitly labeled quote-derived bars.

    Alpaca crypto OHLC includes quote midpoints when no trades occur. The legacy daily
    research path remains trade-only; the opted-in price-feature path retains zero volume.
    See https://docs.alpaca.markets/us/docs/historical-crypto-data-1.
    """
    volume = _decimal(payload, "v", symbol)
    if volume < 0:
        raise MarketDataProtocolError(f"crypto bar {symbol} volume must be nonnegative")
    if volume == 0 and not quote_price_bars:
        return None
    try:
        return Bar(
            symbol=symbol,
            timestamp=_timestamp(payload, symbol),
            open=_decimal(payload, "o", symbol),
            high=_decimal(payload, "h", symbol),
            low=_decimal(payload, "l", symbol),
            close=_decimal(payload, "c", symbol),
            volume=volume,
            adjustment=Decimal("1"),
            **({"price_basis": "ALPACA_CRYPTO_TRADE_OR_QUOTE_V1"} if quote_price_bars else {}),
        )
    except ValidationError as error:
        raise MarketDataProtocolError(f"crypto bar {symbol} failed domain validation") from error


class AlpacaCryptoDataClient:
    """Fetch completed crypto bars, paginating to exhaustion."""

    provider = "alpaca-crypto"

    def __init__(
        self,
        *,
        api_key: SecretStr,
        api_secret: SecretStr,
        transport: AsyncMarketDataTransport,
        url: str = ALPACA_CRYPTO_BARS_URL,
        sleeper: Sleeper | None = None,
        max_retries: int = 2,
        timeout_seconds: Decimal = Decimal("30"),
        quote_price_bars: bool = False,
    ) -> None:
        if not api_key.get_secret_value() or not api_secret.get_secret_value():
            raise ValueError("Alpaca credentials must not be empty")
        if max_retries < 0:
            raise ValueError("max_retries must be nonnegative")
        if not timeout_seconds.is_finite() or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        self._api_key = api_key
        self._api_secret = api_secret
        self._transport = transport
        self._url = url
        self._sleeper = sleeper or _default_sleeper
        self._max_retries = max_retries
        self._timeout_seconds = timeout_seconds
        self.quote_price_bars = quote_price_bars
        #: Count of sessions omitted for recording no trading, so the omission is visible
        #: rather than silent. Downstream validation reports these as gaps.
        self.skipped_untraded_sessions = 0

    def __repr__(self) -> str:
        return (
            "AlpacaCryptoDataClient(api_key=SecretStr('**********'), "
            "api_secret=SecretStr('**********'))"
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._api_key.get_secret_value(),
            "APCA-API-SECRET-KEY": self._api_secret.get_secret_value(),
            "Accept": "application/json",
        }

    @staticmethod
    def _retry_after(headers: Mapping[str, str]) -> Decimal:
        raw = next((value for key, value in headers.items() if key.lower() == "retry-after"), "1")
        try:
            delay = Decimal(raw)
        except InvalidOperation as error:
            raise MarketDataProtocolError("Retry-After header is invalid") from error
        if not delay.is_finite() or delay < 0:
            raise MarketDataProtocolError("Retry-After header is invalid")
        return delay

    async def _request(
        self, params: Mapping[str, str], *, url: str | None = None
    ) -> TransportResponse:
        attempt = 0
        while True:
            try:
                response = await self._transport.request(
                    "GET",
                    url or self._url,
                    headers=self._headers,
                    params=params,
                    timeout_seconds=self._timeout_seconds,
                )
            except MarketDataTransportError as error:
                if attempt >= self._max_retries:
                    raise MarketDataRequestError(
                        "Alpaca crypto transport failed after bounded retries"
                    ) from error
                await self._sleeper(Decimal(2) ** attempt)
                attempt += 1
                continue
            retryable = response.status_code == 429 or 500 <= response.status_code < 600
            if retryable and attempt < self._max_retries:
                delay = (
                    self._retry_after(response.headers)
                    if response.status_code == 429
                    else Decimal(2) ** attempt
                )
                await self._sleeper(delay)
                attempt += 1
                continue
            if not 200 <= response.status_code < 300:
                raise MarketDataRequestError(
                    f"Alpaca crypto request failed with HTTP {response.status_code}"
                )
            return response

    async def get_latest_quotes(self, symbols: tuple[str, ...]) -> dict[str, Mapping[str, object]]:
        """Retain native quote payloads; consumers prove freshness against provider time."""
        if (
            not symbols
            or len(set(symbols)) != len(symbols)
            or any(
                re.fullmatch(r"[A-Z0-9]{2,12}/[A-Z0-9]{2,12}", symbol) is None for symbol in symbols
            )
        ):
            raise ValueError("unique canonical crypto symbols required")
        response = await self._request({"symbols": ",".join(symbols)}, url=ALPACA_CRYPTO_QUOTES_URL)
        try:
            payload = json.loads(response.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MarketDataProtocolError("crypto quotes response is not valid JSON") from error
        quotes = payload.get("quotes") if isinstance(payload, dict) else None
        if not isinstance(quotes, dict) or any(
            symbol not in symbols or not isinstance(raw, dict) for symbol, raw in quotes.items()
        ):
            raise MarketDataProtocolError(
                "crypto quotes response has invalid symbol scope or shape"
            )
        return cast(dict[str, Mapping[str, object]], quotes)

    @staticmethod
    def _page(response: TransportResponse) -> tuple[Mapping[str, object], str | None]:
        try:
            loaded = json.loads(response.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MarketDataProtocolError("crypto response is not valid JSON") from error
        if not isinstance(loaded, dict):
            raise MarketDataProtocolError("crypto response must be a JSON object")
        payload = cast(Mapping[str, object], loaded)
        bars = payload.get("bars")
        if bars is not None and not isinstance(bars, dict):
            raise MarketDataProtocolError("crypto response bars must be an object")
        token = payload.get("next_page_token")
        if token is not None and not isinstance(token, str):
            raise MarketDataProtocolError("crypto next_page_token must be text or absent")
        return cast(Mapping[str, object], bars or {}), token

    def _utc(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    async def get_historical_bars(self, query: BarQuery) -> MarketDataBatch:
        """Fetch every bar in the window, following pagination to exhaustion.

        A partially-read page is the failure mode that silently truncates history and then
        reads as missing data, so pagination is followed until the provider stops offering a
        token, and a repeated token is treated as a protocol error rather than looped on.
        """
        requested = set(query.symbols)
        collected: list[Bar] = []
        seen: set[tuple[str, datetime]] = set()
        seen_tokens: set[str] = set()
        token: str | None = None
        pages = 0
        while True:
            params = {
                "symbols": ",".join(query.symbols),
                "timeframe": query.timeframe,
                "start": self._utc(query.start),
                "end": self._utc(query.end),
                "limit": str(query.limit),
                "sort": "asc",
            }
            if token is not None:
                params["page_token"] = token
            bars, next_token = self._page(await self._request(params))
            pages += 1
            for symbol, rows in bars.items():
                if symbol not in requested:
                    raise MarketDataProtocolError(f"unexpected symbol in crypto response: {symbol}")
                if not isinstance(rows, list):
                    raise MarketDataProtocolError(f"crypto bars for {symbol} must be an array")
                for row in cast(list[object], rows):
                    if not isinstance(row, dict):
                        raise MarketDataProtocolError(f"crypto bar for {symbol} must be an object")
                    bar = _bar(
                        symbol,
                        cast(Mapping[str, object], row),
                        quote_price_bars=self.quote_price_bars,
                    )
                    if bar is None:
                        self.skipped_untraded_sessions += 1
                        continue
                    key = (bar.symbol, bar.timestamp)
                    if key in seen:
                        raise MarketDataProtocolError(
                            f"duplicate crypto bar for {symbol} at {bar.timestamp.isoformat()}"
                        )
                    seen.add(key)
                    collected.append(bar)
            if next_token is None:
                break
            if next_token in seen_tokens:
                raise MarketDataProtocolError("crypto pagination token cycle detected")
            seen_tokens.add(next_token)
            token = next_token

        return MarketDataBatch(
            provider=self.provider,
            feed=MarketDataFeed.CRYPTO,
            adjustment=CRYPTO_ADJUSTMENT,
            timeframe=query.timeframe,
            requested_symbols=query.symbols,
            start=query.start,
            end=query.end,
            bars=tuple(sorted(collected, key=lambda bar: (bar.timestamp, bar.symbol))),
            page_count=pages,
        )
