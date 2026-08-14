"""Concrete HTTP transport for broker REST adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from typing import Protocol, cast

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from quantbot.brokers.alpaca_stream import (
    TradeStreamConnection,
    TradeStreamDisconnected,
)
from quantbot.brokers.base import (
    BrokerTransportError,
    BrokerTransportResponse,
)


class HttpxBrokerTransport:
    """Map the broker-neutral transport contract onto httpx."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient()

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        json_body: Mapping[str, object] | None,
        timeout_seconds: Decimal,
    ) -> BrokerTransportResponse:
        if not timeout_seconds.is_finite() or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        try:
            response = await self._client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=float(timeout_seconds),
            )
        except httpx.TransportError as error:
            raise BrokerTransportError("broker HTTP transport failed") from error
        return BrokerTransportResponse(
            status_code=response.status_code,
            headers=dict(response.headers.items()),
            content=response.content,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class RawTradeWebSocket(Protocol):
    async def recv(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...

    async def close(self) -> None: ...


TradeWebSocketOpener = Callable[[str], Awaitable[RawTradeWebSocket]]


async def _default_trade_opener(url: str) -> RawTradeWebSocket:
    return cast(RawTradeWebSocket, await connect(url))


class WebsocketsTradeConnection:
    """Normalize websocket-library disconnects for the broker stream."""

    def __init__(self, connection: RawTradeWebSocket) -> None:
        self._connection = connection

    async def receive(self) -> str | bytes:
        try:
            return await self._connection.recv()
        except ConnectionClosed as error:
            raise TradeStreamDisconnected("trade websocket disconnected") from error

    async def send(self, message: str) -> None:
        try:
            await self._connection.send(message)
        except ConnectionClosed as error:
            raise TradeStreamDisconnected("trade websocket disconnected") from error

    async def close(self) -> None:
        await self._connection.close()


class WebsocketsTradeConnector:
    """Open Alpaca trade-stream sockets through the production library."""

    def __init__(self, opener: TradeWebSocketOpener | None = None) -> None:
        self._opener = opener or _default_trade_opener

    async def open(self, url: str) -> TradeStreamConnection:
        try:
            raw = await self._opener(url)
        except (OSError, TimeoutError) as error:
            raise TradeStreamDisconnected("trade websocket connection failed") from error
        return WebsocketsTradeConnection(raw)
