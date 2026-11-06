"""Native crypto quote contracts, using only scripted HTTP responses."""

import json
from decimal import Decimal

import pytest
from pydantic import SecretStr

from quantbot.market_data.base import MarketDataProtocolError, TransportResponse
from quantbot.market_data.crypto import AlpacaCryptoDataClient


class QuoteTransport:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return TransportResponse(
            status_code=200, headers={}, content=json.dumps(self.payload).encode()
        )


def client(transport):
    return AlpacaCryptoDataClient(
        api_key=SecretStr("offline"),
        api_secret=SecretStr("offline"),
        transport=transport,
        max_retries=0,
        timeout_seconds=Decimal(2),
    )


async def test_quotes_use_native_endpoint_and_retain_actual_source():
    raw = {"t": "2026-11-07T00:00:00Z", "bp": 69000, "ap": 69001, "bs": 1, "as": 2}
    transport = QuoteTransport({"quotes": {"BTC/USD": raw}})
    result = await client(transport).get_latest_quotes(("BTC/USD", "ETH/USD"))
    assert result == {"BTC/USD": raw}  # Missing ETH is not fabricated or filled forward.
    method, url, kwargs = transport.calls[0]
    assert method == "GET"
    assert url == "https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes"
    assert kwargs["params"] == {"symbols": "BTC/USD,ETH/USD"}


@pytest.mark.parametrize(
    "payload", [[], {"quotes": []}, {"quotes": {"ETH/USD": {}}}, {"quotes": {"BTC/USD": []}}]
)
async def test_quotes_reject_malformed_or_out_of_scope_responses(payload):
    with pytest.raises(MarketDataProtocolError, match="crypto quotes"):
        await client(QuoteTransport(payload)).get_latest_quotes(("BTC/USD",))


@pytest.mark.parametrize("symbols", [(), ("BTC/USD", "BTC/USD"), ("BTCUSD",), ("btc/USD",)])
async def test_quote_request_rejects_ambiguous_symbols_before_transport(symbols):
    transport = QuoteTransport({"quotes": {}})
    with pytest.raises(ValueError, match="crypto symbols"):
        await client(transport).get_latest_quotes(symbols)
    assert not transport.calls
