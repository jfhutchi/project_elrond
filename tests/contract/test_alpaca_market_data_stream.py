from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from decimal import Decimal

import pytest
from pydantic import SecretStr

from quantbot.market_data import (
    AlpacaStockDataStream,
    MarketDataFeed,
    StreamDisconnected,
    StreamProtocolError,
    StreamStaleError,
)

CONNECTED = json.dumps([{"T": "success", "msg": "connected"}])
AUTHENTICATED = json.dumps([{"T": "success", "msg": "authenticated"}])


def raw_stream_bar(symbol: str = "SPY") -> dict[str, object]:
    return {
        "T": "b",
        "S": symbol,
        "t": "2026-08-31T19:59:00Z",
        "o": 650.1,
        "h": 650.5,
        "l": 649.9,
        "c": 650.25,
        "v": 1000,
    }


class ScriptedSocket:
    def __init__(self, incoming: list[str | bytes | Exception | object]) -> None:
        self.incoming = list(incoming)
        self.sent: list[Mapping[str, object]] = []
        self.closed = False

    async def receive(self) -> str | bytes:
        item = self.incoming.pop(0)
        if item is HANG:
            await asyncio.sleep(1)
            raise AssertionError("stale receive should be cancelled")
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, (str, bytes))
        return item

    async def send(self, message: str) -> None:
        decoded = json.loads(message)
        assert isinstance(decoded, dict)
        self.sent.append(decoded)

    async def close(self) -> None:
        self.closed = True


class ScriptedConnector:
    def __init__(self, sockets: list[ScriptedSocket | Exception]) -> None:
        self.sockets = list(sockets)
        self.urls: list[str] = []

    async def open(self, url: str) -> ScriptedSocket:
        self.urls.append(url)
        item = self.sockets.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


HANG = object()


def stream(
    connector: ScriptedConnector,
    *,
    max_reconnects: int = 1,
    stale_after_seconds: Decimal = Decimal("1"),
) -> AlpacaStockDataStream:
    return AlpacaStockDataStream(
        api_key=SecretStr("paper-key"),
        api_secret=SecretStr("paper-secret"),
        feed=MarketDataFeed.IEX,
        connector=connector,
        max_reconnects=max_reconnects,
        stale_after_seconds=stale_after_seconds,
    )


@pytest.mark.asyncio
async def test_stream_authenticates_subscribes_and_decodes_binary_array() -> None:
    socket = ScriptedSocket(
        [
            CONNECTED,
            AUTHENTICATED,
            json.dumps([raw_stream_bar()]).encode("utf-8"),
        ]
    )
    connector = ScriptedConnector([socket])
    generator = stream(connector).stream_bars(("SPY",))

    observed = await anext(generator)
    await generator.aclose()

    assert observed.symbol == "SPY"
    assert observed.close == Decimal("650.25")
    assert connector.urls == ["wss://stream.data.alpaca.markets/v2/iex"]
    assert socket.sent == [
        {"action": "auth", "key": "paper-key", "secret": "paper-secret"},
        {"action": "subscribe", "bars": ["SPY"]},
    ]
    assert socket.closed is True


@pytest.mark.asyncio
async def test_disconnect_reconnects_and_resubscribes_before_yielding() -> None:
    first = ScriptedSocket([CONNECTED, AUTHENTICATED, StreamDisconnected("closed")])
    second = ScriptedSocket([CONNECTED, AUTHENTICATED, json.dumps([raw_stream_bar("QQQ")])])
    connector = ScriptedConnector([first, second])
    generator = stream(connector).stream_bars(("QQQ",))

    observed = await anext(generator)
    await generator.aclose()

    assert observed.symbol == "QQQ"
    assert len(connector.urls) == 2
    assert first.sent[-1] == {"action": "subscribe", "bars": ["QQQ"]}
    assert second.sent[-1] == {"action": "subscribe", "bars": ["QQQ"]}
    assert first.closed is True
    assert second.closed is True


@pytest.mark.asyncio
async def test_connection_open_failure_reconnects_within_bound() -> None:
    socket = ScriptedSocket([CONNECTED, AUTHENTICATED, json.dumps([raw_stream_bar("SPY")])])
    connector = ScriptedConnector([StreamDisconnected("open failed"), socket])
    generator = stream(connector).stream_bars(("SPY",))

    assert (await anext(generator)).symbol == "SPY"
    await generator.aclose()

    assert len(connector.urls) == 2
    assert socket.closed is True


@pytest.mark.asyncio
async def test_stale_stream_fails_closed_after_bounded_reconnects() -> None:
    socket = ScriptedSocket([CONNECTED, AUTHENTICATED, HANG])
    connector = ScriptedConnector([socket])
    generator = stream(
        connector,
        max_reconnects=0,
        stale_after_seconds=Decimal("0.001"),
    ).stream_bars(("SPY",))

    with pytest.raises(StreamStaleError, match="stale"):
        await anext(generator)

    assert socket.closed is True


@pytest.mark.asyncio
async def test_bad_authentication_ack_is_not_retried() -> None:
    socket = ScriptedSocket(
        [CONNECTED, json.dumps([{"T": "error", "code": 401, "msg": "bad auth"}])]
    )
    connector = ScriptedConnector([socket])
    generator = stream(connector).stream_bars(("SPY",))

    with pytest.raises(StreamProtocolError, match="authentication") as captured:
        await anext(generator)

    assert "paper-key" not in str(captured.value)
    assert "paper-secret" not in str(captured.value)
    assert len(connector.urls) == 1
    assert socket.closed is True


def test_stream_rejects_duplicate_or_padded_symbols_and_hides_secrets() -> None:
    instance = stream(ScriptedConnector([]))
    assert "paper-key" not in repr(instance)
    assert "paper-secret" not in repr(instance)
    with pytest.raises(ValueError, match="unique"):
        instance.stream_bars(("SPY", "SPY"))
    with pytest.raises(ValueError, match="uppercase"):
        instance.stream_bars(("spy",))
