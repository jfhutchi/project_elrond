from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quantbot.paper_core.contracts import CoreConfig, Observation, OpeningQuoteSource
from quantbot.paper_core.time_evidence import (
    BrokerTime,
    TimeEvidence,
    TimeSample,
    entry_time_objections,
    project_time_evidence,
)

OPEN = datetime(2026, 10, 14, 13, 30, tzinfo=UTC)


def fixture():
    captured = OPEN + timedelta(seconds=10)
    wall = captured + timedelta(seconds=0.5)
    close = OPEN.replace(hour=20, minute=0) - timedelta(days=1)
    sources = tuple(
        OpeningQuoteSource(
            symbol=symbol,
            previous_session_open=OPEN - timedelta(days=1),
            previous_session_close=close,
            previous_bar_timestamp=close.replace(hour=4),
            previous_close=Decimal(100),
            quote_at=captured - timedelta(seconds=2),
            bid=Decimal(101),
            ask=Decimal("101.01"),
        )
        for symbol in ("XLE", "SPY")
    )
    observation = Observation(
        recorded_at_utc=captured,
        evidence_lane="BROKER_PAPER",
        release_id="fixture",
        symbol="XLE",
        feed="alpaca",
        source_start=close,
        source_end=captured,
        available_at=captured,
        capture_received_at=captured,
        raw_data_digest="actual",
        price=Decimal("101.01"),
        feature_version="opening-quote-gap-v1",
        feature_context="OPENING_QUOTE_GAP_V1",
        opening_quote_sources=sources,
        features=(
            ("gap_return", 0.01005),
            ("market_gap_return", 0.01005),
            ("relative_gap", 0),
            ("spread_bps", 100 / 101),
        ),
        dependencies=("SPY",),
        dependency_available_at=(sources[1].quote_at,),
        sequence=1,
        session_open=OPEN,
        session_close=OPEN.replace(hour=20, minute=0),
    )
    samples = tuple(
        TimeSample(
            provider=BrokerTime(
                timestamp=wall - timedelta(seconds=2.1 - i * 0.1),
                account_id="paper",
                provider="alpaca",
                session_id="2026-10-14",
                session_open=OPEN,
                session_close=observation.session_close,
                resolution_seconds=0.000001,
            ),
            wall_start=wall - timedelta(seconds=0.1 - i * 0.1),
            wall_end=wall - timedelta(seconds=0.1 - i * 0.1),
            mono_start=0.9 + i * 0.1,
            mono_end=0.9 + i * 0.1,
            offset_lower=-2,
            offset_upper=-2,
        )
        for i in range(2)
    )
    evidence = TimeEvidence(
        recorded_at_utc=wall,
        process_id="test",
        samples=samples,
        acquired_monotonic=1,
        wall_anchor=wall,
        offset_lower=-2,
        offset_upper=-2,
        objections=(),
    )
    config = CoreConfig(
        account_id="paper",
        symbol="XLE",
        release_id="fixture",
        evidence_lane="BROKER_PAPER",
        clock_profile="PAPER_TIME_V2",
    )
    return observation, evidence, config


def test_valid_negative_offset_compares_quote_source_to_provider_time():
    observation, evidence, config = fixture()
    assert evidence.wall_anchor + timedelta(seconds=evidence.offset_lower) < observation.source_end
    assert not entry_time_objections(observation, evidence, config)


@pytest.mark.parametrize(
    "case",
    [
        "future_provider_quote",
        "stale_quote",
        "missing_sources",
        "future_capture",
        "naive_quote",
        "mismatched_symbol",
    ],
)
def test_opening_quote_time_fails_closed(case):
    observation, evidence, config = fixture()
    asset, market = observation.opening_quote_sources
    provider_now = evidence.wall_anchor + timedelta(seconds=evidence.offset_lower)
    if case == "missing_sources":
        observation = observation.model_copy(update={"opening_quote_sources": None})
    elif case == "future_capture":
        future = evidence.wall_anchor + timedelta(seconds=0.1)
        observation = observation.model_copy(
            update=dict(
                source_end=future,
                available_at=future,
                capture_received_at=future,
                recorded_at_utc=future,
            )
        )
    else:
        changes = {"quote_at": provider_now + timedelta(seconds=0.1)}
        if case == "stale_quote":
            changes = {"quote_at": provider_now - timedelta(seconds=5.01)}
        elif case == "naive_quote":
            changes = {"quote_at": asset.quote_at.replace(tzinfo=None)}
        elif case == "mismatched_symbol":
            changes = {"symbol": "OTHER"}
        asset = asset.model_copy(update=changes)
        observation = observation.model_copy(update={"opening_quote_sources": (asset, market)})
    assert "TIME_OBSERVATION_BOUNDARY" in entry_time_objections(observation, evidence, config)


def test_market_boundary_and_clock_discontinuity_remain_blocked():
    observation, evidence, config = fixture()
    future_open = OPEN + timedelta(seconds=9)
    changed = tuple(
        s.model_copy(
            update={"provider": s.provider.model_copy(update={"session_open": future_open})}
        )
        for s in evidence.samples
    )
    assert "TIME_SESSION_BOUNDARY" in entry_time_objections(
        observation, evidence.model_copy(update={"samples": changed}), config
    )

    class JumpedClock:
        process_id = "test"

        def sample(self):
            return evidence.wall_anchor + timedelta(seconds=1), evidence.acquired_monotonic

    projection = project_time_evidence(evidence, JumpedClock())
    assert "TIME_DISCONTINUITY" in entry_time_objections(
        observation, evidence, config, projection=projection
    )


def test_regular_completed_bar_time_contract_is_unchanged():
    observation, evidence, config = fixture()
    observation = observation.model_copy(
        update=dict(
            feature_context="REGULAR_SESSION_V1",
            feature_version="five-minute-price-alpha-v1",
            opening_quote_sources=None,
            source_end=OPEN,
            source_start=OPEN - timedelta(minutes=35),
        )
    )
    assert not entry_time_objections(observation, evidence, config)
    future = observation.model_copy(update={"source_end": evidence.wall_anchor})
    assert "TIME_OBSERVATION_BOUNDARY" in entry_time_objections(future, evidence, config)
