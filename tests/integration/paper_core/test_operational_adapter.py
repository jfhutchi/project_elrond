"""Offline provider contracts; none of these fixtures certifies the actual account."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from quantbot.brokers.alpaca import AlpacaPaperBrokerAdapter
from quantbot.brokers.base import BrokerTransportResponse
from quantbot.market_data.alpaca import AlpacaMarketDataClient
from quantbot.market_data.calendar import AlpacaMarketCalendar
from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
from quantbot.paper_core.contracts import CoreConfig
from quantbot.paper_core.release import RuntimeConfig


class ProviderFixture:
    def __init__(self, now):
        self.now, self.calls = now, []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("params")))
        if url.endswith("/calendar"):
            payload = [
                {
                    "date": (self.now.date() + timedelta(days=d)).isoformat(),
                    "open": "09:30",
                    "close": "16:00",
                }
                for d in (-1, 0, 1)
            ]
        elif url.endswith("/clock"):
            payload = {
                "timestamp": self.now.isoformat(),
                "is_open": True,
                "next_open": (self.now + timedelta(days=1)).isoformat(),
                "next_close": self.now.replace(hour=20, minute=0, second=0).isoformat(),
            }
        elif url.endswith("/bars"):
            end = self.now.replace(second=0, microsecond=0)
            payload = {
                "bars": {
                    s: [
                        {
                            "t": (end - timedelta(minutes=35 - i * 5)).isoformat(),
                            "o": 100,
                            "h": 100,
                            "l": 100,
                            "c": 100,
                            "v": 100,
                        }
                        for i in range(7)
                    ]
                    for s in ("XLE", "SPY")
                },
                "next_page_token": None,
            }
        else:
            payload = []
        return BrokerTransportResponse(
            status_code=200, headers={}, content=json.dumps(payload).encode()
        )


@pytest.mark.asyncio
async def test_operational_features_accept_real_receipt_after_bar_boundary(tmp_path):
    from quantbot.market_data.base import TransportResponse

    now = datetime(2026, 10, 14, 14, 5, 3, tzinfo=UTC)
    provider = ProviderFixture(now)

    class MarketTransport:
        async def request(self, *args, **kwargs):
            response = await provider.request(*args, **kwargs)
            return TransportResponse.model_validate(response.model_dump())

    class Clock:
        process_id = "adapter-fixture"

        def sample(self):
            return now, 1.0

    config = RuntimeConfig(
        repository=str(Path(__file__).resolve().parents[3]),
        store_path=str(tmp_path / "fixture.db"),
        store_identity="fixture",
        expected_account_hash=hashlib.sha256(b"paper").hexdigest(),
        core=CoreConfig(
            account_id="paper",
            symbol="XLE",
            release_id="UNBOUND_PREPARED_RELEASE",
            evidence_lane="BROKER_PAPER",
            clock_profile="PAPER_TIME_V2",
        ),
        holdout_source=str(tmp_path / "canonical.db"),
        learning_origin=now - timedelta(days=20),
        canonical_datasets=("us-equities-daily",),
    )
    key, secret = SecretStr("offline-key"), SecretStr("offline-secret")
    broker = AlpacaCoreBroker(
        AlpacaPaperBrokerAdapter(api_key=key, api_secret=secret, transport=provider),
        AlpacaMarketDataClient(api_key=key, api_secret=secret, transport=MarketTransport()),
        AlpacaMarketCalendar(api_key=key, api_secret=secret, transport=MarketTransport()),
        config,
        Clock(),
        account_id="paper",
    )
    observed = await broker.observation()
    assert observed.source_end == now - timedelta(seconds=3)
    assert observed.available_at == observed.recorded_at_utc == now
    assert observed.dependencies == ("SPY",) and len(observed.features) == 10
    assert observed.evidence_lane == "BROKER_PAPER" and observed.feed == "alpaca"
    assert all(call[0] == "GET" for call in provider.calls)
    print("M6_ADAPTER_OBSERVATION:" + observed.model_dump_json())


def test_documented_partial_paper_zero_does_not_become_all_in_zero():
    from quantbot.domain import Fill, OrderSide
    from quantbot.paper_core.operational_costs import PaperCostComponent, PaperCostContract

    now = datetime(2026, 10, 14, 14, 5, tzinfo=UTC)
    component = PaperCostComponent(
        component="REGULATORY",
        document_id="paper-doc-v20261014",
        source_url="https://docs.alpaca.markets/us/docs/paper-trading",
        source_sha256="a" * 64,
        applicable_account_evidence_id="OFFLINE_APPLICABILITY_FIXTURE",
    )
    contract = PaperCostContract(
        account_id="paper",
        endpoint="https://paper-api.alpaca.markets",
        asset_class="US_LISTED_EQUITY",
        symbols=("XLE",),
        start=now - timedelta(days=1),
        end=now + timedelta(days=1),
        components=(component,),
    )
    fill = Fill(
        fill_id="fill",
        broker_order_id="order",
        symbol="XLE",
        side=OrderSide.SELL,
        quantity=1,
        price=100,
        occurred_at=now,
    )
    assert contract.fee(fill, account_id="paper", at=now, release_id="fixture") is None
    assert fill.fee is None
