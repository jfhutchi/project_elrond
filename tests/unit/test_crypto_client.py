"""The crypto bar client, exercised without the network.

It was verified against the live API when written, which proves it works on one day's data
and pins none of its behaviour. These tests cover the logic that only shows up on awkward
input: untraded sessions, pagination, and malformed payloads.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr

from quantbot.market_data import (
    AdjustmentMode,
    AlpacaCryptoDataClient,
    BarQuery,
    MarketDataFeed,
    MarketDataProtocolError,
    MarketDataRequestError,
    TransportResponse,
)
from quantbot.market_data.base import MarketDataTransportError

QUERY = BarQuery(
    symbols=("BTC/USD", "ETH/USD"),
    start=datetime(2026, 8, 1, tzinfo=UTC),
    end=datetime(2026, 8, 3, tzinfo=UTC),
    timeframe="1Day",
    feed=MarketDataFeed.CRYPTO,
    adjustment=AdjustmentMode.RAW,
    limit=1000,
)


def _row(day: int, close: str = "60000", volume: str = "12.5") -> dict[str, Any]:
    return {
        "t": f"2026-08-{day:02d}T00:00:00Z",
        "o": "59000",
        "h": "61000",
        "l": "58000",
        "c": close,
        "v": volume,
        "n": 1000,
        "vw": "59500",
    }


class ScriptedTransport:
    """Serves a fixed list of pages, recording the params it was called with."""

    def __init__(self, pages: list[dict[str, Any]], status_code: int = 200) -> None:
        self.pages = pages
        self.status_code = status_code
        self.calls: list[Mapping[str, str]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        timeout_seconds: Decimal,
    ) -> TransportResponse:
        self.calls.append(dict(params))
        assert "APCA-API-KEY-ID" in headers
        index = min(len(self.calls) - 1, len(self.pages) - 1)
        return TransportResponse(
            status_code=self.status_code,
            headers={},
            content=json.dumps(self.pages[index]).encode("utf-8"),
        )


class ExplodingTransport:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.attempts = 0

    async def request(
        self, method: str, url: str, *, headers: Mapping[str, str],
        params: Mapping[str, str], timeout_seconds: Decimal,
    ) -> TransportResponse:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise MarketDataTransportError("boom")
        return TransportResponse(
            status_code=200,
            headers={},
            content=json.dumps({"bars": {"BTC/USD": [_row(1)]}}).encode("utf-8"),
        )


def _client(transport: Any, **kwargs: Any) -> AlpacaCryptoDataClient:
    return AlpacaCryptoDataClient(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        transport=transport,
        sleeper=_no_sleep,
        **kwargs,
    )


async def _no_sleep(_delay: Decimal) -> None:
    return None


async def test_bars_parse_and_the_batch_names_crypto_honestly() -> None:
    transport = ScriptedTransport([{"bars": {"BTC/USD": [_row(1), _row(2)]}}])

    batch = await _client(transport).get_historical_bars(QUERY)

    assert len(batch.bars) == 2
    assert batch.provider == "alpaca-crypto"
    assert batch.feed is MarketDataFeed.CRYPTO
    assert batch.adjustment is AdjustmentMode.RAW
    assert batch.bars[0].adjustment == Decimal("1")
    assert batch.bars[0].close == Decimal("60000")
    assert batch.page_count == 1


async def test_untraded_sessions_are_omitted_and_counted() -> None:
    """Observed live on SOL/USD: a zero-volume bar's OHLC is carried forward, not observed."""
    transport = ScriptedTransport(
        [{"bars": {"BTC/USD": [_row(1), _row(2, volume="0"), _row(3)]}}]
    )
    client = _client(transport)

    batch = await client.get_historical_bars(QUERY)

    assert len(batch.bars) == 2, "the untraded session must not appear"
    assert client.skipped_untraded_sessions == 1, "and must not vanish silently"


async def test_pagination_is_followed_to_exhaustion() -> None:
    transport = ScriptedTransport([
        {"bars": {"BTC/USD": [_row(1)]}, "next_page_token": "a"},
        {"bars": {"BTC/USD": [_row(2)]}, "next_page_token": "b"},
        {"bars": {"ETH/USD": [_row(3)]}},
    ])

    batch = await _client(transport).get_historical_bars(QUERY)

    assert len(batch.bars) == 3
    assert batch.page_count == 3
    assert transport.calls[1]["page_token"] == "a"
    assert transport.calls[2]["page_token"] == "b"


async def test_a_repeated_page_token_is_a_protocol_error_not_an_infinite_loop() -> None:
    """Distinct bars with a repeated token: duplicate detection would otherwise fire first.

    Both guards prevent the same runaway loop, and duplicate bars are the earlier of the two.
    Isolating the token check needs a provider that advances its data but not its cursor.
    """
    transport = ScriptedTransport([
        {"bars": {"BTC/USD": [_row(1)]}, "next_page_token": "same"},
        {"bars": {"BTC/USD": [_row(2)]}, "next_page_token": "same"},
    ])

    with pytest.raises(MarketDataProtocolError, match="token cycle"):
        await _client(transport).get_historical_bars(QUERY)


async def test_duplicate_bars_are_rejected() -> None:
    transport = ScriptedTransport([{"bars": {"BTC/USD": [_row(1), _row(1)]}}])

    with pytest.raises(MarketDataProtocolError, match="duplicate crypto bar"):
        await _client(transport).get_historical_bars(QUERY)


async def test_an_unrequested_symbol_is_rejected() -> None:
    transport = ScriptedTransport([{"bars": {"DOGE/USD": [_row(1)]}}])

    with pytest.raises(MarketDataProtocolError, match="unexpected symbol"):
        await _client(transport).get_historical_bars(QUERY)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ("not json at all", "not valid JSON"),
        ([1, 2, 3], "must be a JSON object"),
        ({"bars": []}, "bars must be an object"),
        ({"bars": {"BTC/USD": {}}}, "must be an array"),
        ({"bars": {"BTC/USD": ["x"]}}, "must be an object"),
        ({"bars": {"BTC/USD": [_row(1)]}, "next_page_token": 5}, "must be text or absent"),
    ],
)
async def test_malformed_payloads_fail_closed(payload: Any, match: str) -> None:
    class Raw:
        async def request(
            self, method: str, url: str, *, headers: Mapping[str, str],
            params: Mapping[str, str], timeout_seconds: Decimal,
        ) -> TransportResponse:
            body = payload if isinstance(payload, str) else json.dumps(payload)
            return TransportResponse(
                status_code=200, headers={}, content=body.encode("utf-8")
            )

    with pytest.raises(MarketDataProtocolError, match=match):
        await _client(Raw()).get_historical_bars(QUERY)


async def test_a_bar_with_a_missing_field_fails_closed() -> None:
    broken = _row(1)
    del broken["c"]
    transport = ScriptedTransport([{"bars": {"BTC/USD": [broken]}}])

    with pytest.raises(MarketDataProtocolError, match="field c must be numeric"):
        await _client(transport).get_historical_bars(QUERY)


async def test_an_error_status_is_reported_not_swallowed() -> None:
    transport = ScriptedTransport([{"bars": {}}], status_code=403)

    with pytest.raises(MarketDataRequestError, match="HTTP 403"):
        await _client(transport, max_retries=0).get_historical_bars(QUERY)


async def test_transport_failures_retry_then_succeed() -> None:
    transport = ExplodingTransport(failures=2)

    batch = await _client(transport, max_retries=2).get_historical_bars(QUERY)

    assert transport.attempts == 3
    assert len(batch.bars) == 1


async def test_transport_failures_beyond_the_budget_are_reported() -> None:
    transport = ExplodingTransport(failures=5)

    with pytest.raises(MarketDataRequestError, match="bounded retries"):
        await _client(transport, max_retries=1).get_historical_bars(QUERY)


def test_credentials_and_limits_are_validated() -> None:
    transport = ScriptedTransport([{"bars": {}}])
    with pytest.raises(ValueError, match="must not be empty"):
        AlpacaCryptoDataClient(
            api_key=SecretStr(""), api_secret=SecretStr("s"), transport=transport
        )
    with pytest.raises(ValueError, match="max_retries"):
        AlpacaCryptoDataClient(
            api_key=SecretStr("k"), api_secret=SecretStr("s"),
            transport=transport, max_retries=-1,
        )


def test_repr_never_leaks_credentials() -> None:
    text = repr(_client(ScriptedTransport([{"bars": {}}])))

    assert "key" not in text.replace("api_key", "").replace("SecretStr", "")
    assert "**********" in text
