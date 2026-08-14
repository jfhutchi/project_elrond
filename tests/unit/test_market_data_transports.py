from __future__ import annotations

from collections.abc import Awaitable
from decimal import Decimal

import httpx
import pytest

from quantbot.market_data import (
    HttpxMarketDataTransport,
    MarketDataTransportError,
    StreamDisconnected,
    WebsocketsConnector,
)


@pytest.mark.asyncio
async def test_httpx_transport_preserves_response_and_request_contract() -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["method"] = request.method
        observed["url"] = str(request.url)
        observed["key"] = request.headers["APCA-API-KEY-ID"]
        return httpx.Response(200, headers={"X-Test": "yes"}, content=b'{"bars":{}}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as raw_client:
        transport = HttpxMarketDataTransport(client=raw_client)
        response = await transport.request(
            "GET",
            "https://data.alpaca.markets/v2/stocks/bars",
            headers={"APCA-API-KEY-ID": "key"},
            params={"symbols": "SPY"},
            timeout_seconds=Decimal("2.5"),
        )
        await transport.aclose()

    assert observed == {
        "method": "GET",
        "url": "https://data.alpaca.markets/v2/stocks/bars?symbols=SPY",
        "key": "key",
    }
    assert response.status_code == 200
    assert response.headers["x-test"] == "yes"
    assert response.content == b'{"bars":{}}'
    assert raw_client.is_closed is True


class RawSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def recv(self) -> str | bytes:
        return b"[]"

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_websockets_connector_wraps_send_receive_and_close() -> None:
    raw = RawSocket()
    urls: list[str] = []

    def opener(url: str) -> Awaitable[RawSocket]:
        urls.append(url)

        async def opened() -> RawSocket:
            return raw

        return opened()

    connector = WebsocketsConnector(opener=opener)
    connection = await connector.open("wss://stream.data.alpaca.markets/v2/iex")
    assert await connection.receive() == b"[]"
    await connection.send('{"action":"subscribe"}')
    await connection.close()

    assert urls == ["wss://stream.data.alpaca.markets/v2/iex"]
    assert raw.sent == ['{"action":"subscribe"}']
    assert raw.closed is True


@pytest.mark.asyncio
async def test_httpx_transport_rejects_nonpositive_timeout() -> None:
    transport = HttpxMarketDataTransport()
    with pytest.raises(ValueError, match="positive"):
        await transport.request(
            "GET",
            "https://example.invalid",
            headers={},
            params={},
            timeout_seconds=Decimal("0"),
        )
    await transport.aclose()


@pytest.mark.asyncio
async def test_httpx_transport_normalizes_connectivity_failures() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as raw_client:
        transport = HttpxMarketDataTransport(client=raw_client)
        with pytest.raises(MarketDataTransportError, match="transport") as captured:
            await transport.request(
                "GET",
                "https://data.alpaca.markets/v2/stocks/bars",
                headers={},
                params={},
                timeout_seconds=Decimal("1"),
            )

    assert "offline" not in str(captured.value)


@pytest.mark.asyncio
async def test_websocket_connector_normalizes_open_failures() -> None:
    def opener(_url: str) -> Awaitable[RawSocket]:
        async def failed() -> RawSocket:
            raise OSError("offline")

        return failed()

    with pytest.raises(StreamDisconnected, match="connection failed") as captured:
        await WebsocketsConnector(opener=opener).open("wss://example.invalid")

    assert "offline" not in str(captured.value)
