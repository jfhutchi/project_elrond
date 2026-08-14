from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr

from quantbot.market_data import (
    AdjustmentMode,
    AlpacaMarketDataClient,
    BarQuery,
    MarketDataFeed,
    MarketDataProtocolError,
    MarketDataRequestError,
    MarketDataTransportError,
    MarketSessionClose,
    TransportResponse,
)


def response(
    status: int,
    payload: Mapping[str, object],
    *,
    headers: Mapping[str, str] | None = None,
) -> TransportResponse:
    return TransportResponse(
        status_code=status,
        headers=dict(headers or {}),
        content=json.dumps(payload, separators=(",", ":")).encode("ascii"),
    )


def raw_bar(timestamp: str, close: str) -> dict[str, object]:
    value = Decimal(close)
    return {
        "t": timestamp,
        "o": str(value - Decimal("1")),
        "h": str(value + Decimal("1")),
        "l": str(value - Decimal("2")),
        "c": close,
        "v": "1000",
        "n": 100,
        "vw": close,
    }


class ScriptedTransport:
    def __init__(self, scripted: list[TransportResponse | Exception]) -> None:
        self.scripted = list(scripted)
        self.calls: list[dict[str, object]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        timeout_seconds: Decimal,
    ) -> TransportResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "params": dict(params),
                "timeout_seconds": timeout_seconds,
            }
        )
        item = self.scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def query(symbols: tuple[str, ...] = ("SPY", "QQQ")) -> BarQuery:
    return BarQuery(
        symbols=symbols,
        start=datetime(2026, 8, 27, 20, tzinfo=UTC),
        end=datetime(2026, 8, 31, 20, tzinfo=UTC),
        timeframe="1Day",
        feed=MarketDataFeed.IEX,
        adjustment=AdjustmentMode.ALL,
        limit=10000,
    )


def client(
    transport: ScriptedTransport,
    *,
    sleeper: Any | None = None,
    max_retries: int = 2,
) -> AlpacaMarketDataClient:
    return AlpacaMarketDataClient(
        api_key=SecretStr("paper-key"),
        api_secret=SecretStr("paper-secret"),
        transport=transport,
        sleeper=sleeper,
        max_retries=max_retries,
    )


@pytest.mark.asyncio
async def test_historical_bars_authenticate_paginate_and_parse_decimals() -> None:
    transport = ScriptedTransport(
        [
            response(
                200,
                {
                    "bars": {
                        "QQQ": [raw_bar("2026-08-28T04:00:00Z", "570.25")],
                    },
                    "next_page_token": "page-two",
                },
            ),
            response(
                200,
                {
                    "bars": {
                        "SPY": [raw_bar("2026-08-28T04:00:00Z", "650.50")],
                    },
                    "next_page_token": None,
                },
            ),
        ]
    )

    batch = await client(transport).get_historical_bars(query())

    assert batch.provider == "alpaca"
    assert batch.feed is MarketDataFeed.IEX
    assert batch.adjustment is AdjustmentMode.ALL
    assert batch.page_count == 2
    assert tuple((bar.symbol, bar.close) for bar in batch.bars) == (
        ("QQQ", Decimal("570.25")),
        ("SPY", Decimal("650.50")),
    )
    assert all(isinstance(bar.close, Decimal) for bar in batch.bars)
    assert len(transport.calls) == 2
    first, second = transport.calls
    assert first["method"] == "GET"
    assert first["url"] == "https://data.alpaca.markets/v2/stocks/bars"
    assert first["headers"] == {
        "APCA-API-KEY-ID": "paper-key",
        "APCA-API-SECRET-KEY": "paper-secret",
        "Accept": "application/json",
    }
    assert first["params"] == {
        "symbols": "QQQ,SPY",
        "timeframe": "1Day",
        "start": "2026-08-27T20:00:00Z",
        "end": "2026-08-31T20:00:00Z",
        "limit": "10000",
        "adjustment": "all",
        "feed": "iex",
        "sort": "asc",
    }
    assert second["params"] == {**first["params"], "page_token": "page-two"}
    assert "paper-key" not in repr(client(transport))
    assert "paper-secret" not in repr(client(transport))


@pytest.mark.asyncio
async def test_latest_completed_daily_bars_excludes_future_and_returns_each_symbol() -> None:
    transport = ScriptedTransport(
        [
            response(
                200,
                {
                    "bars": {
                        "QQQ": [
                            raw_bar("2026-08-28T04:00:00Z", "570"),
                            raw_bar("2026-09-01T04:00:00Z", "580"),
                        ],
                        "SPY": [
                            raw_bar("2026-08-28T04:00:00Z", "650"),
                            raw_bar("2026-09-01T04:00:00Z", "660"),
                        ],
                    },
                    "next_page_token": None,
                },
            )
        ]
    )

    latest = await client(transport).get_latest_completed_daily_bars(
        query(),
        completed_through=datetime(2026, 8, 31, 20, tzinfo=UTC),
        sessions=(
            MarketSessionClose(
                calendar="XNYS",
                session_date=date(2026, 8, 28),
                close_at=datetime(2026, 8, 28, 20, tzinfo=UTC),
            ),
            MarketSessionClose(
                calendar="XNYS",
                session_date=date(2026, 9, 1),
                close_at=datetime(2026, 9, 1, 20, tzinfo=UTC),
            ),
        ),
    )

    assert tuple(bar.symbol for bar in latest) == ("QQQ", "SPY")
    assert all(bar.timestamp <= datetime(2026, 8, 31, 20, tzinfo=UTC) for bar in latest)
    assert tuple(bar.close for bar in latest) == (Decimal("570"), Decimal("650"))


@pytest.mark.asyncio
async def test_latest_completed_daily_bars_excludes_current_forming_session() -> None:
    transport = ScriptedTransport(
        [
            response(
                200,
                {
                    "bars": {
                        "SPY": [
                            raw_bar("2026-08-28T04:00:00Z", "650"),
                            raw_bar("2026-08-31T04:00:00Z", "655"),
                        ],
                    },
                    "next_page_token": None,
                },
            )
        ]
    )

    latest = await client(transport).get_latest_completed_daily_bars(
        query(("SPY",)),
        completed_through=datetime(2026, 8, 31, 15, tzinfo=UTC),
        sessions=(
            MarketSessionClose(
                calendar="XNYS",
                session_date=date(2026, 8, 28),
                close_at=datetime(2026, 8, 28, 20, tzinfo=UTC),
            ),
            MarketSessionClose(
                calendar="XNYS",
                session_date=date(2026, 8, 31),
                close_at=datetime(2026, 8, 31, 20, tzinfo=UTC),
            ),
        ),
    )

    assert tuple((bar.symbol, bar.timestamp, bar.close) for bar in latest) == (
        ("SPY", datetime(2026, 8, 28, 20, tzinfo=UTC), Decimal("650")),
    )


@pytest.mark.asyncio
async def test_latest_completed_daily_bars_rejects_missing_requested_symbol() -> None:
    transport = ScriptedTransport(
        [
            response(
                200,
                {
                    "bars": {"SPY": [raw_bar("2026-08-28T04:00:00Z", "650")]},
                    "next_page_token": None,
                },
            )
        ]
    )
    sessions = (
        MarketSessionClose(
            calendar="XNYS",
            session_date=date(2026, 8, 28),
            close_at=datetime(2026, 8, 28, 20, tzinfo=UTC),
        ),
    )

    with pytest.raises(MarketDataProtocolError, match="QQQ"):
        await client(transport).get_latest_completed_daily_bars(
            query(),
            completed_through=datetime(2026, 8, 28, 20, tzinfo=UTC),
            sessions=sessions,
        )


@pytest.mark.asyncio
async def test_json_numbers_never_pass_through_binary_float() -> None:
    transport = ScriptedTransport(
        [
            TransportResponse(
                status_code=200,
                headers={},
                content=(
                    b'{"bars":{"SPY":[{"t":"2026-08-28T04:00:00Z",'
                    b'"o":100.0000000000000001,"h":101.0000000000000001,'
                    b'"l":99.0000000000000001,"c":100.1000000000000001,'
                    b'"v":1000}]},"next_page_token":null}'
                ),
            )
        ]
    )

    observed = await client(transport).get_historical_bars(query(("SPY",)))

    assert observed.bars[0].close == Decimal("100.1000000000000001")


@pytest.mark.asyncio
async def test_rate_limit_uses_retry_after_before_retrying() -> None:
    sleeps: list[Decimal] = []

    async def sleeper(delay: Decimal) -> None:
        sleeps.append(delay)

    transport = ScriptedTransport(
        [
            response(429, {"message": "rate limited"}, headers={"Retry-After": "2.5"}),
            response(200, {"bars": {}, "next_page_token": None}),
        ]
    )

    await client(transport, sleeper=sleeper).get_historical_bars(query())

    assert sleeps == [Decimal("2.5")]
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_server_errors_retry_only_within_bound() -> None:
    sleeps: list[Decimal] = []

    async def sleeper(delay: Decimal) -> None:
        sleeps.append(delay)

    transport = ScriptedTransport(
        [
            response(500, {"message": "one"}),
            response(503, {"message": "two"}),
            response(200, {"bars": {}, "next_page_token": None}),
        ]
    )
    await client(transport, sleeper=sleeper, max_retries=2).get_historical_bars(query())
    assert len(transport.calls) == 3
    assert sleeps == [Decimal("1"), Decimal("2")]

    exhausted = ScriptedTransport(
        [
            response(500, {"message": "one"}),
            response(503, {"message": "two"}),
            response(502, {"message": "three"}),
        ]
    )
    with pytest.raises(MarketDataRequestError, match="502"):
        await client(exhausted, sleeper=sleeper, max_retries=2).get_historical_bars(query())
    assert len(exhausted.calls) == 3


@pytest.mark.asyncio
async def test_terminal_client_error_is_not_retried_or_secret_bearing() -> None:
    transport = ScriptedTransport([response(401, {"message": "unauthorized"})])

    with pytest.raises(MarketDataRequestError) as captured:
        await client(transport).get_historical_bars(query())

    assert len(transport.calls) == 1
    message = str(captured.value)
    assert "401" in message
    assert "paper-key" not in message
    assert "paper-secret" not in message


@pytest.mark.asyncio
async def test_repeated_pagination_token_fails_explicitly() -> None:
    transport = ScriptedTransport(
        [
            response(200, {"bars": {}, "next_page_token": "again"}),
            response(200, {"bars": {}, "next_page_token": "again"}),
        ]
    )

    with pytest.raises(MarketDataProtocolError, match="pagination token cycle"):
        await client(transport).get_historical_bars(query())


@pytest.mark.asyncio
async def test_transport_outage_retries_within_bound_then_fails_safely() -> None:
    sleeps: list[Decimal] = []

    async def sleeper(delay: Decimal) -> None:
        sleeps.append(delay)

    recovered = ScriptedTransport(
        [
            MarketDataTransportError("socket failed"),
            response(200, {"bars": {}, "next_page_token": None}),
        ]
    )
    await client(recovered, sleeper=sleeper, max_retries=1).get_historical_bars(query())
    assert sleeps == [Decimal("1")]

    exhausted = ScriptedTransport(
        [MarketDataTransportError("one"), MarketDataTransportError("two")]
    )
    with pytest.raises(MarketDataRequestError, match="transport") as captured:
        await client(exhausted, sleeper=sleeper, max_retries=1).get_historical_bars(query())
    assert "one" not in str(captured.value)
    assert "two" not in str(captured.value)
