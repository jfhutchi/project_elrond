"""A retained comparison can retry late bars without refreshing its quote authority."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from tests.unit.paper_core.test_crypto_broker_data import broker_fixture

from quantbot.paper_core.discovery import DiscoveryStore
from quantbot.paper_core.entry_policy import EntryPolicy

NOW = datetime(2026, 11, 6, 20, 25, 3, tzinfo=UTC)


def snapshot(at, *, ask=100):
    return {
        "latestQuote": {"t": at.isoformat(), "ap": ask, "bp": ask - 0.01, "as": 100, "bs": 100},
        "minuteBar": {"t": at.isoformat(), "v": 10000},
        "dailyBar": {"c": ask, "v": 1000000},
        "prevDailyBar": {"c": 99},
    }


def discovery(tmp_path):
    state = DiscoveryStore(tmp_path / "discovery.db")
    state.record_catalog(
        [
            {"symbol": symbol, "class": "us_equity", "status": "active", "tradable": True}
            for symbol in ("XLE", "MSFT")
        ],
        now=NOW,
    )
    state.record_scan(
        ("XLE",), {"XLE": snapshot(NOW - timedelta(seconds=25))}, now=NOW, market_open=True
    )
    return state


async def test_empty_bars_retry_after_15_seconds_reuses_original_comparison(tmp_path):
    state = discovery(tmp_path)
    broker, provider = broker_fixture(NOW)
    broker.config.entry_policy = EntryPolicy(
        contract="RESOURCE_BOUNDED_PAPER_V1", universe="ALPACA_ASSETS_V1", symbols=()
    )
    broker.clocks = SimpleNamespace(sample=lambda: (provider.now, 10.0))
    broker._discovery_state = state
    provider.market_open = True
    provider.missing = ("SPY", 6)
    first = await broker.observation_cohort()
    assert not first.observations
    assert first.unavailable == (("XLE", "FRESH_COMPLETE_BAR_UNAVAILABLE"),)
    original = first.discovery
    assert original is not None and original.symbols == ("XLE",)
    with state.connection() as connection:
        retained = connection.execute("select boundary,payload from comparisons").fetchall()
        compared = connection.execute("select compared from samples where symbol='XLE'").fetchone()
    provider.now += timedelta(seconds=15)
    provider.missing = None
    # A newer, higher-scoring scan must not replace the existing boundary's ownership.
    state.record_scan(
        ("MSFT",),
        {"MSFT": snapshot(provider.now, ask=200)},
        now=provider.now,
        market_open=True,
    )
    retried = await broker.observation_cohort()
    assert tuple(o.symbol for o in retried.observations) == ("XLE",), retried.unavailable
    assert not retried.unavailable
    assert retried.source_end == first.source_end
    assert retried.discovery == original and retried.discovery.id == original.id
    assert retried.discovery.recorded_at == NOW
    assert retried.discovery.samples[0].source_at == NOW - timedelta(seconds=25)
    assert retried.discovery.samples[0].received_at == NOW
    # Repeated attempts retain one comparison and never charge comparison ownership again.
    assert (await broker.observation_cohort()).discovery == original
    with state.connection() as connection:
        assert connection.execute("select boundary,payload from comparisons").fetchall() == retained
        assert (
            connection.execute("select compared from samples where symbol='XLE'").fetchone()
            == compared
        )


@pytest.mark.parametrize("elapsed,allowed", [(65, True), (65.001, False), (91, False)])
def test_retained_comparison_keeps_original_90_second_quote_limit(tmp_path, elapsed, allowed):
    state = discovery(tmp_path)
    original = state.select_candidates(now=NOW, boundary="original")
    later = NOW + timedelta(seconds=elapsed)
    if allowed:
        assert state.select_candidates(now=later, boundary="original") == original
    else:
        with pytest.raises(ValueError, match="^DISCOVERY_RECEIPT_STALE$"):
            state.select_candidates(now=later, boundary="original")
    with state.connection() as connection:
        assert connection.execute("select count(*) from comparisons").fetchone() == (1,)


def test_new_comparison_still_refuses_quotes_older_than_30_seconds(tmp_path):
    state = discovery(tmp_path)
    with pytest.raises(ValueError, match="^DISCOVERY_NO_FRESH_ELIGIBLE$"):
        state.select_candidates(now=NOW + timedelta(seconds=6), boundary="new")
    with state.connection() as connection:
        assert connection.execute("select count(*) from comparisons").fetchone() == (0,)
