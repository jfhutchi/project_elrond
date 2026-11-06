"""Offline real-adapter protocol tests of explicit opening context and data absence."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError
from tests.integration.paper_core.test_operational_adapter import ProviderFixture

from quantbot.brokers.alpaca import AlpacaPaperBrokerAdapter
from quantbot.market_data.alpaca import AlpacaMarketDataClient
from quantbot.market_data.base import TransportResponse
from quantbot.market_data.calendar import AlpacaMarketCalendar
from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
from quantbot.paper_core.contracts import CoreConfig, Observation
from quantbot.paper_core.release import RuntimeConfig

OPEN = datetime(2026, 10, 14, 13, 30, tzinfo=UTC)


def adapter(tmp_path, minute, missing=False, *, gap=False, quote_age=0, daily_missing=False):
    now = OPEN + timedelta(minutes=minute, seconds=1)
    provider = ProviderFixture(now)

    class Market:
        async def request(self, method, url, **kwargs):
            response = await provider.request(method, url, **kwargs)
            if gap and url.endswith("/quotes/latest"):
                payload = {
                    "quotes": {
                        symbol: {
                            "bp": price,
                            "ap": price + 0.01,
                            "t": (now - timedelta(seconds=quote_age)).isoformat(),
                        }
                        for symbol, price in (("XLE", 102), ("SPY", 200))
                    }
                }
                response = response.model_copy(update={"content": json.dumps(payload).encode()})
            elif gap and url.endswith("/bars") and kwargs["params"]["timeframe"] == "1Day":
                stamp = (OPEN - timedelta(days=1)).replace(hour=4, minute=0)
                payload = {
                    "bars": {}
                    if daily_missing
                    else {
                        symbol: [
                            {
                                "t": stamp.isoformat(),
                                "o": price,
                                "h": price,
                                "l": price,
                                "c": price,
                                "v": 10000,
                            }
                        ]
                        for symbol, price in (("XLE", 100), ("SPY", 200))
                    },
                    "next_page_token": None,
                }
                response = response.model_copy(update={"content": json.dumps(payload).encode()})
            elif missing and url.endswith("/bars"):
                payload = json.loads(response.content)
                payload["bars"]["SPY"] = payload["bars"]["SPY"][1:]
                response = response.model_copy(update={"content": json.dumps(payload).encode()})
            return TransportResponse.model_validate(response.model_dump())

    class Clock:
        process_id = "opening-offline"

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
    key = SecretStr("OFFLINE_FIXTURE")
    return AlpacaCoreBroker(
        AlpacaPaperBrokerAdapter(api_key=key, api_secret=key, transport=provider),
        AlpacaMarketDataClient(api_key=key, api_secret=key, transport=Market()),
        AlpacaMarketCalendar(api_key=key, api_secret=key, transport=Market()),
        config,
        Clock(),
        account_id="paper",
    )


@pytest.mark.asyncio
async def test_sparse_premarket_uses_actual_daily_reference_and_fresh_quotes(tmp_path):
    broker = adapter(tmp_path, 0, missing=True, gap=True)
    cohort = await broker._acquire_cohort(("XLE",), verify_assets=False)
    (observation,) = cohort.observations
    assert observation.feature_context == "OPENING_QUOTE_GAP_V1"
    assert observation.feature_version == "opening-quote-gap-v1"
    assert observation.source_end == OPEN + timedelta(seconds=1)
    assert observation.source_start == OPEN.replace(hour=20, minute=0) - timedelta(days=1)
    assert float(observation.price) == 102.01
    assert dict(observation.features)["gap_return"] == pytest.approx(102.005 / 100 - 1)
    asset, market = observation.opening_quote_sources
    assert asset.symbol == "XLE" and market.symbol == "SPY"
    assert asset.quote_at == observation.source_end
    assert asset.previous_close == 100
    assert Observation.model_validate_json(observation.model_dump_json()) == observation
    provider = broker.adapter._transport
    before = sum(bool(call[2] and call[2].get("timeframe") == "1Day") for call in provider.calls)
    await broker._acquire_cohort(("XLE",), verify_assets=False)
    after = sum(bool(call[2] and call[2].get("timeframe") == "1Day") for call in provider.calls)
    assert before == after == 1, "Repeated evaluation must not redownload the same daily closes"
    assert sum("/quotes/latest" in call[1] for call in provider.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [-1, 5.01])
async def test_quote_gap_never_relabels_stale_or_future_quotes(tmp_path, age):
    broker = adapter(tmp_path, 0, missing=True, gap=True, quote_age=age)
    cohort = await broker._acquire_cohort(("XLE",), verify_assets=False)
    assert not cohort.observations
    assert cohort.unavailable == (("XLE", "OPENING_QUOTES_UNAVAILABLE"),)
    assert cohort.source_end == OPEN
    from quantbot.paper_core.selection import validate_cohort

    validate_cohort(broker.config.core, cohort)


@pytest.mark.asyncio
async def test_quote_gap_requires_actual_previous_daily_closes(tmp_path):
    cohort = await adapter(tmp_path, 0, missing=True, gap=True, daily_missing=True)._acquire_cohort(
        ("XLE",), verify_assets=False
    )
    assert not cohort.observations
    assert cohort.unavailable == (("XLE", "OPENING_HISTORY_UNAVAILABLE"),)


@pytest.mark.asyncio
@pytest.mark.parametrize("minute", [0, 5, 30, 35])
async def test_adapter_records_actual_opening_context_and_regular_transition(tmp_path, minute):
    broker = adapter(tmp_path, minute)
    cohort = await broker._acquire_cohort(("XLE",), verify_assets=False)
    assert len(cohort.observations) == 1, cohort.unavailable
    observation = cohort.observations[0]
    expected = "OPENING_CONTIGUOUS_V1" if minute < 35 else "REGULAR_SESSION_V1"
    assert observation.feature_context == expected
    assert observation.source_end == OPEN + timedelta(minutes=minute)
    assert observation.source_start == observation.source_end - timedelta(minutes=35)
    if minute == 35:
        assert "feature_context" not in observation.model_dump()
        assert Observation.model_validate_json(observation.model_dump_json()).id == observation.id
    else:
        assert observation.model_dump()["feature_context"] == expected
        raw = observation.model_dump()
        raw["source_start"] -= timedelta(minutes=5)
        with pytest.raises(ValidationError, match="opening"):
            Observation.model_validate(raw)


@pytest.mark.asyncio
async def test_missing_opening_history_is_explained_without_fabrication(tmp_path):
    cohort = await adapter(tmp_path, 0, missing=True)._acquire_cohort(("XLE",), verify_assets=False)
    assert not cohort.observations
    assert cohort.unavailable == (("XLE", "OPENING_HISTORY_UNAVAILABLE"),)
