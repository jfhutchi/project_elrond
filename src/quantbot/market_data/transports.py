"""Concrete HTTP and websocket transports for Alpaca market data."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from typing import Protocol, cast

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from quantbot.market_data.base import MarketDataTransportError, TransportResponse
from quantbot.market_data.stream import StreamDisconnected, WebSocketConnection


class HttpxMarketDataTransport:
    """Map the provider-neutral request contract onto an async HTTP client."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient()

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        timeout_seconds: Decimal,
    ) -> TransportResponse:
        if not timeout_seconds.is_finite() or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        try:
            response = await self._client.request(
                method,
                url,
                headers=headers,
                params=params,
                timeout=float(timeout_seconds),
            )
        except httpx.TransportError as error:
            raise MarketDataTransportError("HTTP market-data transport failed") from error
        return TransportResponse(
            status_code=response.status_code,
            headers=dict(response.headers.items()),
            content=response.content,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class RawWebSocket(Protocol):
    async def recv(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...

    async def close(self) -> None: ...


WebSocketOpener = Callable[[str], Awaitable[RawWebSocket]]


async def _default_opener(url: str) -> RawWebSocket:
    return cast(RawWebSocket, await connect(url))


class WebsocketsConnection:
    """Translate library disconnect exceptions into the stable stream contract."""

    def __init__(self, connection: RawWebSocket) -> None:
        self._connection = connection

    async def receive(self) -> str | bytes:
        try:
            return await self._connection.recv()
        except ConnectionClosed as error:
            raise StreamDisconnected("stock-data websocket disconnected") from error

    async def send(self, message: str) -> None:
        try:
            await self._connection.send(message)
        except ConnectionClosed as error:
            raise StreamDisconnected("stock-data websocket disconnected") from error

    async def close(self) -> None:
        await self._connection.close()


class WebsocketsConnector:
    """Open Alpaca websocket URLs using the production websocket library."""

    def __init__(self, opener: WebSocketOpener | None = None) -> None:
        self._opener = opener or _default_opener

    async def open(self, url: str) -> WebSocketConnection:
        try:
            raw = await self._opener(url)
        except (OSError, TimeoutError) as error:
            raise StreamDisconnected("stock-data websocket connection failed") from error
        return WebsocketsConnection(raw)
