from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from quantbot.brokers import BrokerTransportError, HttpxBrokerTransport


@pytest.mark.asyncio
async def test_httpx_broker_transport_preserves_request_and_response_contract() -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["method"] = request.method
        observed["url"] = str(request.url)
        observed["body"] = request.content
        observed["key"] = request.headers["APCA-API-KEY-ID"]
        return httpx.Response(200, headers={"X-Request-ID": "request-1"}, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as raw_client:
        transport = HttpxBrokerTransport(client=raw_client)
        response = await transport.request(
            "POST",
            "https://paper-api.alpaca.markets/v2/orders",
            headers={"APCA-API-KEY-ID": "paper-key"},
            params={},
            json_body={"symbol": "SPY", "qty": "1"},
            timeout_seconds=Decimal("2.5"),
        )
        await transport.aclose()

    assert observed == {
        "method": "POST",
        "url": "https://paper-api.alpaca.markets/v2/orders",
        "body": b'{"symbol":"SPY","qty":"1"}',
        "key": "paper-key",
    }
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "request-1"
    assert response.content == b'{"ok":true}'
    assert raw_client.is_closed is True


@pytest.mark.asyncio
async def test_httpx_broker_transport_normalizes_connectivity_errors() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("offline", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as raw_client:
        transport = HttpxBrokerTransport(client=raw_client)
        with pytest.raises(BrokerTransportError, match="transport") as captured:
            await transport.request(
                "GET",
                "https://paper-api.alpaca.markets/v2/account",
                headers={},
                params={},
                json_body=None,
                timeout_seconds=Decimal("1"),
            )

    assert "offline" not in str(captured.value)


@pytest.mark.asyncio
async def test_httpx_broker_transport_rejects_nonpositive_timeout() -> None:
    transport = HttpxBrokerTransport()
    with pytest.raises(ValueError, match="positive"):
        await transport.request(
            "GET",
            "https://paper-api.alpaca.markets/v2/account",
            headers={},
            params={},
            json_body=None,
            timeout_seconds=Decimal("0"),
        )
    await transport.aclose()
