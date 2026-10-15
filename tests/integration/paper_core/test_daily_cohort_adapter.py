"""Offline multi-symbol provider acquisition and role plumbing."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr
from tests.integration.paper_core.test_operational_adapter import ProviderFixture

from quantbot.brokers.alpaca import AlpacaPaperBrokerAdapter
from quantbot.brokers.base import BrokerTransportResponse
from quantbot.market_data.alpaca import AlpacaMarketDataClient
from quantbot.market_data.base import TransportResponse
from quantbot.market_data.calendar import AlpacaMarketCalendar
from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
from quantbot.paper_core.contracts import CoreConfig
from quantbot.paper_core.release import RuntimeConfig


@pytest.mark.asyncio
async def test_one_bounded_bar_cohort_preserves_missing_and_restricted_members(tmp_path):
    now = datetime(2026, 10, 14, 14, 5, 3, tzinfo=UTC)

    class Provider(ProviderFixture):
        async def request(self, method, url, **kwargs):
            if "/assets/" in url:
                self.calls.append((method, url, kwargs.get("params")))
                symbol = url.rsplit("/", 1)[-1]
                return BrokerTransportResponse(
                    status_code=200,
                    headers={},
                    content=json.dumps(
                        {
                            "symbol": symbol,
                            "class": "us_equity",
                            "status": "active",
                            "tradable": True,
                            "fractionable": symbol != "NVDA",
                        }
                    ).encode(),
                )
            response = await super().request(method, url, **kwargs)
            if url.endswith("/bars"):
                payload = json.loads(response.content)
                payload["bars"]["AAPL"] = payload["bars"].pop("XLE")
                return response.model_copy(update={"content": json.dumps(payload).encode()})
            return response

    provider = Provider(now)

    class Market:
        async def request(self, *args, **kwargs):
            return TransportResponse.model_validate(
                (await provider.request(*args, **kwargs)).model_dump()
            )

    class Clock:
        process_id = "daily-cohort"

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
            symbols=("AAPL", "MSFT", "NVDA"),
            selection_policy="SIGNAL_OR_EXPLORE_V1",
            release_id="UNBOUND_PREPARED_RELEASE",
            evidence_lane="BROKER_PAPER",
            clock_profile="PAPER_TIME_V2",
        ),
        holdout_source=str(tmp_path / "canonical.db"),
        learning_origin=now - timedelta(days=20),
        canonical_datasets=("us-equities-daily",),
    )
    key = SecretStr("offline")
    broker = AlpacaCoreBroker(
        AlpacaPaperBrokerAdapter(api_key=key, api_secret=key, transport=provider),
        AlpacaMarketDataClient(api_key=key, api_secret=key, transport=Market()),
        AlpacaMarketCalendar(api_key=key, api_secret=key, transport=Market()),
        config,
        Clock(),
        account_id="paper",
    )
    captured = await broker.observation_cohort()
    assert [o.symbol for o in captured.observations] == ["AAPL"]
    assert dict(captured.unavailable) == {
        "MSFT": "FRESH_COMPLETE_BAR_UNAVAILABLE",
        "NVDA": "ASSET_NOT_FRACTIONABLE",
    }
    bar_calls = [c for c in provider.calls if c[1].endswith("/bars")]
    assert len(bar_calls) == 1
    assert set(bar_calls[0][2]["symbols"].split(",")) == {"AAPL", "MSFT", "NVDA", "SPY"}
    assert captured.observations[0].available_at == now
    assert captured.source_end == now - timedelta(seconds=3)


@pytest.mark.asyncio
async def test_trader_role_acquires_cohort_and_guardian_refreshes_owned_actual_symbol(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from tests.integration.paper_core.test_daily_selection import NOW, cohort, prepare

    from quantbot.paper_core import roles
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, model, active = prepare(tmp_path)

    class Clock:
        process_id = "role-cohort"

        def sample(self):
            return NOW, 100.0

    store.clocks = Clock()
    captured = cohort()

    async def acquire():
        return captured

    broker.observation_cohort = acquire

    async def legacy():
        raise AssertionError("hybrid role used legacy single-symbol acquisition")

    broker.observation = legacy
    runtime = SimpleNamespace(
        entry_poll_seconds=15, guardian_poll_seconds=15, outcomes_poll_seconds=15
    )
    try:
        await roles.run_role(store, broker, runtime, "trader", cycles=1)
        assert any(
            d["symbol"] == "AAPL" and d.get("selection", {}).get("kind") == "EXPLORATION"
            for d in store.documents("core_decisions")
        )
        # Dispatch waits for a fresh independent guardian; execute a separate synthetic lot below.
        store.runtime_role = None
        # A fresh disposable fixture gives the guardian a real owned filled lot.
    finally:
        db.close()
    other = tmp_path / "owned"
    other.mkdir()
    db, store, broker, proof, model, active = prepare(other)
    try:
        store.clocks = Clock()
        decision = await trader_cycle(store, broker, captured, model, proof, now=NOW)
        assert decision.symbol == "AAPL"
        refreshed = []

        async def refresh(symbol):
            refreshed.append(symbol)

        broker.refresh_mark = refresh
        monkeypatch.setattr(roles, "AlpacaCoreBroker", type(broker))
        await roles.run_role(store, broker, runtime, "guardian", cycles=1)
        assert refreshed == ["AAPL"]
    finally:
        db.close()


def test_cost_contract_must_cover_whole_trade_universe(tmp_path):
    from quantbot.paper_core.operational_costs import PaperCostContract

    now = datetime(2026, 10, 14, 14, 5, tzinfo=UTC)
    contract = PaperCostContract(
        account_id="paper",
        endpoint="https://paper-api.alpaca.markets",
        asset_class="US_LISTED_EQUITY",
        symbols=("XLE",),
        start=now - timedelta(days=1),
        end=now + timedelta(days=1),
        components=(),
    )
    with pytest.raises(ValueError, match="cost contract"):
        RuntimeConfig(
            repository=str(tmp_path),
            store_path=str(tmp_path / "s.db"),
            store_identity="fixture",
            expected_account_hash=hashlib.sha256(b"paper").hexdigest(),
            core=CoreConfig(
                account_id="paper",
                symbol="XLE",
                symbols=("AAPL", "MSFT"),
                selection_policy="SIGNAL_OR_EXPLORE_V1",
                clock_profile="PAPER_TIME_V2",
                release_id="UNBOUND_PREPARED_RELEASE",
            ),
            holdout_source=str(tmp_path / "h.db"),
            learning_origin=now,
            cost_contract=contract,
        )
