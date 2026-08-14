"""Alpaca stock-bar streaming with bounded reconnect and stale detection."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Mapping
from decimal import Decimal
from typing import Protocol, cast

from pydantic import SecretStr, ValidationError

from quantbot.domain import Bar
from quantbot.market_data.alpaca import _parse_decimal, _parse_timestamp
from quantbot.market_data.base import MarketDataFeed


class StreamError(RuntimeError):
    """Base error for stock-data streaming."""


class StreamDisconnected(StreamError):
    """Raised by a connection adapter after a websocket disconnect."""


class StreamStaleError(StreamError):
    """Raised when no websocket frame arrives within the configured bound."""


class StreamProtocolError(StreamError):
    """Raised when the websocket protocol cannot be verified."""


class WebSocketConnection(Protocol):
    async def receive(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...

    async def close(self) -> None: ...


class WebSocketConnector(Protocol):
    async def open(self, url: str) -> WebSocketConnection: ...


def _decode_frame(frame: str | bytes) -> tuple[Mapping[str, object], ...]:
    if isinstance(frame, bytes):
        try:
            text = frame.decode("utf-8")
        except UnicodeDecodeError as error:
            raise StreamProtocolError("stream frame is not UTF-8 JSON") from error
    else:
        text = frame
    try:
        loaded = json.loads(text, parse_float=Decimal, parse_int=Decimal)
    except json.JSONDecodeError as error:
        raise StreamProtocolError("stream frame is not valid JSON") from error
    if not isinstance(loaded, list) or not loaded:
        raise StreamProtocolError("stream frame must be a nonempty JSON array")
    messages: list[Mapping[str, object]] = []
    for item in loaded:
        if not isinstance(item, dict):
            raise StreamProtocolError("stream message must be a JSON object")
        messages.append(cast(Mapping[str, object], item))
    return tuple(messages)


def _expect_success(frame: str | bytes, expected: str, operation: str) -> None:
    messages = _decode_frame(frame)
    if len(messages) != 1:
        raise StreamProtocolError(f"stream {operation} acknowledgement is invalid")
    message = messages[0]
    if message.get("T") != "success" or message.get("msg") != expected:
        raise StreamProtocolError(f"stream {operation} failed")


def _parse_stream_bar(message: Mapping[str, object]) -> Bar:
    symbol = message.get("S")
    if not isinstance(symbol, str):
        raise StreamProtocolError("stream bar symbol must be a string")
    try:
        return Bar(
            symbol=symbol,
            timestamp=_parse_timestamp(message.get("t")),
            open=_parse_decimal(message.get("o"), "open"),
            high=_parse_decimal(message.get("h"), "high"),
            low=_parse_decimal(message.get("l"), "low"),
            close=_parse_decimal(message.get("c"), "close"),
            volume=_parse_decimal(message.get("v"), "volume"),
            adjustment=Decimal("1"),
        )
    except ValidationError as error:
        raise StreamProtocolError("stream bar failed domain validation") from error


class AlpacaStockDataStream:
    """Authenticate, subscribe, and restore stock-bar streams after disconnects."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        api_secret: SecretStr,
        feed: MarketDataFeed,
        connector: WebSocketConnector,
        stale_after_seconds: Decimal = Decimal("30"),
        max_reconnects: int = 5,
    ) -> None:
        if not api_key.get_secret_value() or not api_secret.get_secret_value():
            raise ValueError("Alpaca credentials must not be empty")
        if not stale_after_seconds.is_finite() or stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive and finite")
        if max_reconnects < 0:
            raise ValueError("max_reconnects must be nonnegative")
        self._api_key = api_key
        self._api_secret = api_secret
        self._feed = feed
        self._connector = connector
        self._stale_after_seconds = stale_after_seconds
        self._max_reconnects = max_reconnects

    def __repr__(self) -> str:
        return (
            f"AlpacaStockDataStream(feed={self._feed.value!r}, "
            "api_key=SecretStr('**********'), api_secret=SecretStr('**********'))"
        )

    async def _receive(self, connection: WebSocketConnection) -> str | bytes:
        try:
            return await asyncio.wait_for(
                connection.receive(),
                timeout=float(self._stale_after_seconds),
            )
        except TimeoutError as error:
            raise StreamStaleError("stock-data stream is stale") from error

    async def _authenticate(
        self,
        connection: WebSocketConnection,
        symbols: tuple[str, ...],
    ) -> None:
        _expect_success(await self._receive(connection), "connected", "connection")
        await connection.send(
            json.dumps(
                {
                    "action": "auth",
                    "key": self._api_key.get_secret_value(),
                    "secret": self._api_secret.get_secret_value(),
                },
                separators=(",", ":"),
            )
        )
        _expect_success(await self._receive(connection), "authenticated", "authentication")
        await connection.send(
            json.dumps(
                {"action": "subscribe", "bars": list(symbols)},
                separators=(",", ":"),
            )
        )

    @staticmethod
    def _validate_symbols(symbols: tuple[str, ...]) -> tuple[str, ...]:
        if not symbols:
            raise ValueError("stream symbols must not be empty")
        if len(symbols) != len(set(symbols)):
            raise ValueError("stream symbols must be unique")
        if any(
            not symbol or symbol != symbol.strip() or symbol != symbol.upper() for symbol in symbols
        ):
            raise ValueError("stream symbols must be uppercase without surrounding whitespace")
        return tuple(sorted(symbols))

    def stream_bars(self, symbols: tuple[str, ...]) -> AsyncGenerator[Bar, None]:
        """Return an async iterator that reconnects and resubscribes within a fixed bound."""
        validated = self._validate_symbols(symbols)

        async def iterate() -> AsyncGenerator[Bar, None]:
            reconnects = 0
            while True:
                url = f"wss://stream.data.alpaca.markets/v2/{self._feed.value}"
                connection: WebSocketConnection | None = None
                try:
                    connection = await self._connector.open(url)
                    await self._authenticate(connection, validated)
                    while True:
                        frame = await self._receive(connection)
                        for message in _decode_frame(frame):
                            message_type = message.get("T")
                            if message_type == "b":
                                yield _parse_stream_bar(message)
                            elif message_type == "error":
                                raise StreamProtocolError("stock-data stream reported an error")
                            elif message_type not in {"success", "subscription"}:
                                raise StreamProtocolError(
                                    f"unexpected stock-data stream message type: {message_type!r}"
                                )
                except (StreamDisconnected, StreamStaleError):
                    if reconnects >= self._max_reconnects:
                        raise
                    reconnects += 1
                finally:
                    if connection is not None:
                        await connection.close()

        return iterate()
