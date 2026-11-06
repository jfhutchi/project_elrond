"""Provider quote-derived crypto OHLC preserves its zero traded volume and provenance."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from quantbot.domain import Bar
from quantbot.market_data import MarketDataProtocolError
from quantbot.paper_core.contracts import Observation
from quantbot.research.manifest import content_id
from tests.unit.paper_core.test_crypto_broker_data import broker_fixture
from tests.unit.test_crypto_client import QUERY, ScriptedTransport, _client, _row

BASIS = "ALPACA_CRYPTO_TRADE_OR_QUOTE_V1"


def bar_payload(symbol="BTC/USD", volume="0"):
    return dict(
        symbol=symbol,
        timestamp=datetime(2026, 11, 6, tzinfo=UTC),
        open=Decimal(100),
        high=Decimal(102),
        low=Decimal(99),
        close=Decimal(101),
        volume=Decimal(volume),
        adjustment=Decimal(1),
    )


def test_explicit_crypto_price_basis_retains_zero_volume_and_legacy_hash():
    payload = bar_payload(volume="3")
    legacy = Bar(**payload)
    assert content_id(legacy) == content_id(payload)
    assert "price_basis" not in legacy.model_dump()
    quoted = Bar(**bar_payload(), price_basis=BASIS)
    assert quoted.volume == 0 and quoted.price_basis == BASIS
    assert Bar.model_validate_json(quoted.model_dump_json()) == quoted
    assert content_id(quoted) != content_id(bar_payload())


@pytest.mark.parametrize(
    "symbol,basis", [("BTC/USD", None), ("AAPL", BASIS), ("BTC", BASIS), ("BTC/EUR", BASIS)]
)
def test_zero_volume_requires_explicit_usd_crypto_basis(symbol, basis):
    with pytest.raises(ValidationError):
        Bar(**bar_payload(symbol), **({"price_basis": basis} if basis else {}))


async def test_opt_in_crypto_client_retains_provider_zero_volume_and_marks_all_bars():
    transport = ScriptedTransport([{"bars": {"BTC/USD": [_row(1), _row(2, volume="0")]}}])
    client = _client(transport, quote_price_bars=True)
    batch = await client.get_historical_bars(QUERY)
    assert [bar.volume for bar in batch.bars] == [Decimal("12.5"), Decimal(0)]
    assert all(bar.price_basis == BASIS for bar in batch.bars)
    assert client.skipped_untraded_sessions == 0


@pytest.mark.parametrize("opt_in", [False, True])
async def test_negative_provider_volume_is_invalid_not_missing(opt_in):
    transport = ScriptedTransport([{"bars": {"BTC/USD": [_row(1, volume="-1")]}}])
    with pytest.raises(MarketDataProtocolError, match="volume"):
        await _client(transport, quote_price_bars=opt_in).get_historical_bars(QUERY)


async def test_seven_actual_provider_stamps_with_zero_trades_reach_crypto_observation():
    broker, provider = broker_fixture()
    provider.zero_volume = {
        ("BTC/USD", 0),
        ("BTC/USD", 2),
        ("BTC/USD", 5),
        ("ETH/USD", 0),
        ("ETH/USD", 5),
        ("SOL/USD", 0),
        ("SOL/USD", 1),
        ("SOL/USD", 5),
        ("SOL/USD", 6),
    }
    cohort = await broker.acquire_crypto_cohort()
    assert not cohort.unavailable
    assert len(cohort.observations) == 3
    for observation in cohort.observations:
        assert observation.price_basis == BASIS
        assert observation.model_dump()["price_basis"] == BASIS
        assert Observation.model_validate_json(observation.model_dump_json()).id == observation.id
        assert dict(observation.features)["relative_strength_15m"] == 0
    # The provider's nine zero-volume intervals are observed, not invented or omitted.
    assert broker.crypto_market.skipped_untraded_sessions == 0


async def test_legacy_observation_identity_omits_price_basis():
    broker, _ = broker_fixture()
    observation = (await broker.acquire_crypto_cohort()).observations[0]
    payload = observation.model_dump()
    payload.pop("price_basis", None)
    legacy = Observation.model_validate(payload)
    assert "price_basis" not in legacy.model_dump()
    assert legacy.id == content_id(payload)


async def test_quote_derived_bars_keep_the_existing_freshness_guard():
    broker, provider = broker_fixture(datetime(2026, 11, 7, 0, 2, 1, tzinfo=UTC))
    provider.zero_volume = {("BTC/USD", 0)}
    cohort = await broker.acquire_crypto_cohort(("BTC/USD",))
    assert not cohort.observations
    assert cohort.unavailable == (("BTC/USD", "CRYPTO_OBSERVATION_STALE"),)
