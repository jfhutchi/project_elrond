from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quantbot.paper_core.contracts import (
    CoreConfig,
    Observation,
    ObservationCohort,
    OpeningQuoteSource,
)
from quantbot.paper_core.discovery import DiscoveryEvidence, DiscoverySample
from quantbot.paper_core.entry_policy import EntryPolicy
from quantbot.paper_core.features import observation_objections
from quantbot.paper_core.selection import boundary_id, validate_cohort


def quote_observation():
    capture = datetime(2026, 11, 6, 14, 30, 2, tzinfo=UTC)
    common = dict(
        previous_session_open=datetime(2026, 11, 5, 14, 30, tzinfo=UTC),
        previous_session_close=datetime(2026, 11, 5, 21, tzinfo=UTC),
        previous_bar_timestamp=datetime(2026, 11, 5, 5, tzinfo=UTC),
        quote_at=capture - timedelta(seconds=1),
    )
    asset = OpeningQuoteSource(
        symbol="AAPL", previous_close="100", bid="101", ask="101.01", **common
    )
    market = OpeningQuoteSource(
        symbol="SPY", previous_close="500", bid="500", ask="500.01", **common
    )
    gap = (asset.bid + asset.ask) / 2 / asset.previous_close - 1
    market_gap = (market.bid + market.ask) / 2 / market.previous_close - 1
    return Observation(
        recorded_at_utc=capture,
        symbol="AAPL",
        source_start=asset.previous_session_close,
        source_end=capture,
        available_at=capture,
        capture_received_at=capture,
        session_open=capture.replace(second=0),
        session_close=capture.replace(hour=21, minute=0, second=0),
        raw_data_digest="quote",
        price=asset.ask,
        sequence=1,
        feature_version="opening-quote-gap-v1",
        feature_context="OPENING_QUOTE_GAP_V1",
        features=(
            ("gap_return", float(gap)),
            ("market_gap_return", float(market_gap)),
            ("relative_gap", float(gap - market_gap)),
            ("spread_bps", float((asset.ask - asset.bid) / asset.bid * 10000)),
        ),
        opening_quote_sources=(asset, market),
        dependencies=("SPY",),
        dependency_available_at=(market.quote_at,),
    )


def cohort(observation):
    return ObservationCohort(
        source_end=observation.source_end,
        session_open=observation.session_open,
        session_close=observation.session_close,
        observations=(observation,),
    )


def test_valid_quote_sources_and_regular_observations():
    observation = quote_observation()
    assert observation_objections(observation) == ()
    regular = observation.model_copy(
        update={
            "feature_context": "REGULAR_SESSION_V1",
            "feature_version": "synthetic-causal-v1",
            "opening_quote_sources": None,
        }
    )
    assert observation_objections(regular) == ()


@pytest.mark.parametrize(
    "change",
    [
        {"opening_quote_sources": None},
        {"feature_version": "synthetic-causal-v1"},
        {"feature_context": "REGULAR_SESSION_V1"},
        {"price": Decimal("102")},
        {"features": (("gap_return", 0.5),)},
        {"dependencies": ("QQQ",)},
    ],
)
def test_quote_contract_tampering_rejected(change):
    assert "OPENING_CONTEXT_INVALID" in observation_objections(
        quote_observation().model_copy(update=change)
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("symbol", "MSFT"),
        ("previous_session_close", datetime(2026, 11, 6, 21, tzinfo=UTC)),
        ("previous_bar_timestamp", datetime(2026, 11, 6, 5, tzinfo=UTC)),
        ("ask", Decimal("100")),
    ],
)
def test_invalid_source_rejected(field, value):
    observation = quote_observation()
    asset, market = observation.opening_quote_sources
    changed = observation.model_copy(
        update={"opening_quote_sources": (asset.model_copy(update={field: value}), market)}
    )
    assert "OPENING_CONTEXT_INVALID" in observation_objections(changed)


@pytest.mark.parametrize("delta", [-6, 1])
def test_stale_or_future_quote_rejected(delta):
    observation = quote_observation()
    asset, market = observation.opening_quote_sources
    changed = observation.model_copy(
        update={
            "opening_quote_sources": (
                asset.model_copy(
                    update={"quote_at": observation.recorded_at_utc + timedelta(seconds=delta)}
                ),
                market,
            )
        }
    )
    assert "OPENING_QUOTES_UNAVAILABLE" in observation_objections(changed)


def test_quote_expires_at_evaluation_despite_recent_capture():
    observation = quote_observation()
    assert "OPENING_QUOTES_UNAVAILABLE" in observation_objections(
        observation, evaluation_at=observation.recorded_at_utc + timedelta(seconds=5)
    )


@pytest.mark.parametrize("offset", [-1, 1])
def test_verified_provider_projection_controls_quote_age(offset):
    observation = quote_observation()
    evaluated = observation.recorded_at_utc + timedelta(seconds=0.1)
    provider_now = evaluated + timedelta(seconds=offset)
    asset, market = observation.opening_quote_sources
    sources = tuple(
        s.model_copy(update={"quote_at": provider_now - timedelta(seconds=0.05)})
        for s in (asset, market)
    )
    observation = observation.model_copy(
        update={"opening_quote_sources": sources, "dependency_available_at": (sources[1].quote_at,)}
    )
    interval = (provider_now, provider_now)
    assert (
        observation_objections(observation, evaluation_at=evaluated, provider_interval=interval)
        == ()
    )
    if offset > 0:
        assert "OPENING_QUOTES_UNAVAILABLE" in observation_objections(
            observation, evaluation_at=evaluated
        )
    for delta in (-6, 1):
        altered = tuple(
            s.model_copy(update={"quote_at": provider_now + timedelta(seconds=delta)})
            for s in sources
        )
        changed = observation.model_copy(
            update={
                "opening_quote_sources": altered,
                "dependency_available_at": (altered[1].quote_at,),
            }
        )
        assert "OPENING_QUOTES_UNAVAILABLE" in observation_objections(
            changed, evaluation_at=evaluated, provider_interval=interval
        )


def test_quote_boundary_deduplicates_captures_and_rejects_mixed_context():
    observation = quote_observation()
    config = CoreConfig(account_id="paper", symbol="AAPL")
    first = cohort(observation)
    later = first.model_copy(update={"source_end": first.source_end + timedelta(seconds=20)})
    assert boundary_id(config, first) == boundary_id(config, later)
    assert boundary_id(config, first) != boundary_id(
        config, first.model_copy(update={"source_end": first.source_end + timedelta(minutes=5)})
    )
    aligned = observation.source_end.replace(second=0)
    quote = observation.model_copy(update={"source_end": aligned})
    regular = quote.model_copy(update={"symbol": "MSFT", "feature_context": "REGULAR_SESSION_V1"})
    mixed = cohort(quote).model_copy(update={"observations": (quote, regular)})
    with pytest.raises(ValueError, match="verified boundary"):
        validate_cohort(config.model_copy(update={"symbols": ("AAPL", "MSFT")}), mixed)


@pytest.mark.parametrize("stale", [None, "sample", "catalog"])
def test_discovery_freshness_uses_quote_capture(stale):
    observation = quote_observation()
    capture = observation.source_end
    recorded = capture - timedelta(seconds=20)
    sample_at = recorded - timedelta(seconds=80 if stale == "sample" else 10)
    evidence = DiscoveryEvidence(
        catalog_id="catalog",
        catalog_at=recorded - timedelta(seconds=3590 if stale == "catalog" else 30),
        eligible_count=1,
        recorded_at=recorded,
        symbols=("AAPL",),
        scan_receipt_ids=("scan",),
        samples=(
            DiscoverySample(
                symbol="AAPL",
                source_at=sample_at,
                received_at=sample_at,
                dollar_volume=100000,
                spread_bps=1,
                score=1,
                raw_digest="raw",
                scan_receipt_id="scan",
            ),
        ),
    )
    captured = cohort(observation).model_copy(update={"discovery": evidence})
    policy = EntryPolicy(
        symbols=(), contract="RESOURCE_BOUNDED_PAPER_V1", universe="ALPACA_ASSETS_V1"
    )
    config = CoreConfig(account_id="paper", symbol="AAPL")
    if stale:
        with pytest.raises(ValueError, match="fresh captured"):
            validate_cohort(config, captured, entry_policy=policy)
    else:
        validate_cohort(config, captured, entry_policy=policy)
