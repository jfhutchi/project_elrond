"""What wakes the research system up, and the ways a detector invents work for it (#56 phase 1).

The engine's value is "better questions per unit search cost". Every failure mode below costs
that directly: a false trigger spends enrichment on nothing, a duplicated event double-counts one
piece of information, and a baseline contaminated by its own outliers moves the threshold without
anyone choosing to.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from quantbot.anomaly import (
    AnomalyDetectorConfig,
    DataQuality,
    MarketAnomalyEvent,
    PriceObservation,
    SessionObservation,
    SessionSegment,
    TriggerReason,
    VolumeCoverage,
    VolumeObservation,
    detect,
    group_events,
    refusal,
    supports_volume_trigger,
    volume_coverage,
)
from quantbot.market_data.base import MarketDataFeed

NOW = datetime(2026, 8, 28, 20, 5, tzinfo=UTC)
START = date(2026, 1, 5)


def sessions(
    volumes: list[int], *, closes: list[float] | None = None
) -> list[SessionObservation]:
    """A contiguous run of sessions, oldest first, with the last one under judgement."""
    prices = closes or [100.0] * len(volumes)
    return [
        SessionObservation(
            session_date=START + timedelta(days=index),
            volume=Decimal(volume),
            close=Decimal(str(price)),
        )
        for index, (volume, price) in enumerate(zip(volumes, prices, strict=True))
    ]


def quiet(count: int = 60, *, volume: int = 1_000_000) -> list[int]:
    """A baseline that varies a little, so a robust z is defined."""
    return [volume + (index % 7) * 1_000 for index in range(count)]


def config(**overrides: object) -> AnomalyDetectorConfig:
    fields: dict[str, object] = {"feed": MarketDataFeed.SIP, "baseline_sessions": 60}
    fields.update(overrides)
    return AnomalyDetectorConfig(**fields)  # type: ignore[arg-type]


# --- feed capability ----------------------------------------------------------------------------


def test_a_venue_feed_cannot_emit_a_volume_shock() -> None:
    """The measurement that changed the design, enforced rather than documented.

    Robust z on log volume over 2021-2026, threshold 4.0, IEX against SIP: precision 36.6%,
    recall 22.8%, on a venue whose share of consolidated volume drifted 3.6x over the window.
    Two of every three IEX triggers are not real shocks.

    #56 asks that feed semantics be persisted so capability is never read as full-market volume.
    Recording the feed honestly on a bad event still leaves a bad event and still spends
    enrichment on it, so capability gates the trigger instead of labelling it.
    """
    history = sessions([*quiet(), 40_000_000])

    assert detect(symbol="SPY", history=history, config=config(feed=MarketDataFeed.IEX),
                  observed_at=NOW) is None
    assert detect(symbol="SPY", history=history, config=config(feed=MarketDataFeed.SIP),
                  observed_at=NOW) is not None

    reason = refusal(config(feed=MarketDataFeed.IEX), history)
    assert reason is not None and "single venue" in reason


def test_an_uncharacterised_feed_refuses_rather_than_being_assumed_consolidated() -> None:
    """The permissive direction here is assuming coverage nobody measured."""
    assert volume_coverage(MarketDataFeed.SIP) is VolumeCoverage.CONSOLIDATED
    assert volume_coverage(MarketDataFeed.IEX) is VolumeCoverage.SINGLE_VENUE
    assert supports_volume_trigger(MarketDataFeed.IEX) is False
    # Crypto has no consolidated tape to be a fraction of, so venue volume is the whole truth.
    assert supports_volume_trigger(MarketDataFeed.CRYPTO) is True


# --- baselines ----------------------------------------------------------------------------------


def test_one_prior_outlier_does_not_swallow_the_next_real_shock() -> None:
    """Why median and MAD rather than mean and standard deviation.

    A single extreme prior session inflates both the mean and the deviation, so the next genuine
    shock scores lower against a contaminated baseline than against a clean one -- the detector
    quietly raises its own threshold, and nobody chose that.
    """
    clean = sessions([*quiet(), 20_000_000])
    contaminated_base = quiet()
    contaminated_base[10] = 200_000_000
    contaminated = sessions([*contaminated_base, 20_000_000])

    first = detect(symbol="SPY", history=clean, config=config(), observed_at=NOW)
    second = detect(symbol="SPY", history=contaminated, config=config(), observed_at=NOW)

    assert first is not None
    assert second is not None, "one outlier in sixty sessions suppressed a real shock"
    assert first.volume.robust_z is not None and second.volume.robust_z is not None
    # The estimator is not perfectly invariant, but it must not collapse.
    assert second.volume.robust_z > first.volume.robust_z * Decimal("0.9")


def test_a_short_baseline_refuses_rather_than_measuring_on_what_it_has() -> None:
    """A baseline over twelve sessions and one over sixty are not the same evidence."""
    history = sessions([*quiet(20), 40_000_000])

    assert detect(symbol="SPY", history=history, config=config(), observed_at=NOW) is None
    reason = refusal(config(), history)
    assert reason is not None and "different evidence" in reason


def test_a_non_varying_baseline_yields_no_event_rather_than_an_infinite_one() -> None:
    """A z against zero dispersion is undefined, and a large number would be an invention."""
    flat = sessions([*[1_000_000] * 60, 40_000_000])

    assert detect(symbol="SPY", history=flat, config=config(), observed_at=NOW) is None


def test_the_baseline_can_only_contain_sessions_that_already_completed() -> None:
    """Look-ahead is unexpressible by construction, and asserted anyway.

    The observation under judgement is the last element of its own history rather than a
    separate argument, so there is no second parameter that could accidentally carry a later
    session. The model validator is the belt to that braces.
    """
    history = sessions([*quiet(), 40_000_000])
    event = detect(symbol="SPY", history=history, config=config(), observed_at=NOW)

    assert event is not None
    assert max(event.baseline_sessions) < event.session_date

    with pytest.raises(ValueError, match="look-ahead"):
        MarketAnomalyEvent(
            symbol="SPY",
            session_date=date(2026, 3, 2),
            segment=SessionSegment.FULL_SESSION,
            observed_at=NOW,
            detector_identity="anomaly-detector-v1-x",
            feed=MarketDataFeed.SIP,
            volume_coverage=VolumeCoverage.CONSOLIDATED,
            cadence=event.cadence,
            baseline_sessions=(date(2026, 3, 1), date(2026, 3, 3)),
            volume=VolumeObservation(observed=Decimal("1"), expected=Decimal("1")),
            price=PriceObservation(session_return=Decimal("0")),
            triggers=(TriggerReason.VOLUME_SHOCK,),
        )


# --- identity -----------------------------------------------------------------------------------


def test_an_event_id_is_replay_stable_across_clocks() -> None:
    """A retry must not mint a second event for one piece of information.

    That is sample inflation, and #57 deduplicates on exactly this seam. `observed_at` is
    therefore excluded from the identity.
    """
    history = sessions([*quiet(), 40_000_000])
    first = detect(symbol="SPY", history=history, config=config(), observed_at=NOW)
    later = detect(
        symbol="SPY",
        history=history,
        config=config(),
        observed_at=datetime(2030, 1, 1, tzinfo=UTC),
    )

    assert first is not None and later is not None
    assert first.event_id == later.event_id
    assert first.observed_at != later.observed_at


def test_the_identity_moves_with_everything_that_changes_what_fired() -> None:
    """Two events across a detector, a feed or a baseline are two events, not one repeated."""
    history = sessions([*quiet(), 40_000_000])
    base = detect(symbol="SPY", history=history, config=config(), observed_at=NOW)
    assert base is not None

    tighter = detect(
        symbol="SPY",
        history=history,
        config=config(volume_robust_z=Decimal("2.0")),
        observed_at=NOW,
    )
    assert tighter is not None
    assert tighter.event_id != base.event_id, "a different detector is different evidence"

    shorter = detect(
        symbol="SPY",
        history=sessions([*quiet(70), 40_000_000])[10:],
        config=config(),
        observed_at=NOW,
    )
    assert shorter is not None
    assert shorter.event_id != base.event_id, "a different baseline window is a different sample"


def test_the_detector_config_identity_covers_every_seed_threshold() -> None:
    """#56 labels the thresholds research seeds, so nothing measured under one may pool."""
    base = config()
    assert base.identity.startswith("anomaly-detector-v1-")
    for change in (
        {"volume_robust_z": Decimal("3.0")},
        {"interval_rvol": Decimal("2.0")},
        {"price_velocity_sigma": Decimal("3.0")},
        {"baseline_sessions": 90},
        {"feed": MarketDataFeed.DELAYED_SIP},
        {"version": 2},
    ):
        assert config(**change).identity != base.identity, change


# --- grouping -----------------------------------------------------------------------------------


def test_a_market_wide_shock_carries_its_group_so_it_is_not_many_samples() -> None:
    """Measured on the real bars: 53% of events land on sessions where >=5 of 23 symbols fire.

    2025-04-07 fired 22 of 23; the 2020 COVID cluster fires 19-21 repeatedly. Counting those as
    independent observations is a multiple-testing error wearing the costume of a queueing
    problem, so the cluster the #58 dependence snapshot already computed travels with the event.
    """
    history = sessions([*quiet(), 40_000_000])
    events = tuple(
        e
        for symbol in ("SPY", "QQQ", "TLT")
        if (e := detect(symbol=symbol, history=history, config=config(), observed_at=NOW))
    )
    assert len(events) == 3

    grouped = group_events(events, {"SPY": "cluster-a", "QQQ": "cluster-a"})
    by_symbol = {event.symbol: event for event in grouped}

    assert by_symbol["SPY"].group_key == "cluster-a"
    assert by_symbol["QQQ"].group_key == "cluster-a"
    assert by_symbol["TLT"].group_key is None, (
        "a symbol the dependence snapshot could not measure must record no group rather than "
        "being defaulted into one of its own"
    )


# --- the event ----------------------------------------------------------------------------------


def test_an_event_keeps_observed_and_expected_rather_than_only_their_ratio() -> None:
    """A ratio alone cannot be audited: 3.0 could be a quiet baseline or a real surge."""
    history = sessions([*quiet(), 40_000_000])
    event = detect(symbol="SPY", history=history, config=config(), observed_at=NOW)

    assert event is not None
    assert event.volume.observed == Decimal("40000000")
    assert event.volume.expected is not None and event.volume.expected > 0
    assert event.volume.rvol is not None and event.volume.rvol > 3
    assert event.volume.robust_z is not None
    assert event.data_quality is DataQuality.OK
    assert TriggerReason.VOLUME_SHOCK in event.triggers


def test_an_event_records_the_feed_semantics_it_was_measured_under() -> None:
    """So a venue feed can never be read downstream as full-market volume."""
    history = sessions([*quiet(), 40_000_000])
    event = detect(symbol="SPY", history=history, config=config(), observed_at=NOW)

    assert event is not None
    assert event.feed is MarketDataFeed.SIP
    assert event.volume_coverage is VolumeCoverage.CONSOLIDATED
    row = event.as_row()
    assert row["volume_coverage"] == "CONSOLIDATED"
    assert row["baseline_session_count"] == 60


def test_an_event_cannot_be_built_without_a_trigger() -> None:
    """An event with no reason is a row that says something happened without saying what."""
    with pytest.raises(ValueError):
        MarketAnomalyEvent(
            symbol="SPY",
            session_date=date(2026, 3, 2),
            segment=SessionSegment.FULL_SESSION,
            observed_at=NOW,
            detector_identity="anomaly-detector-v1-x",
            feed=MarketDataFeed.SIP,
            volume_coverage=VolumeCoverage.CONSOLIDATED,
            cadence=config().cadence,
            baseline_sessions=(date(2026, 3, 1),),
            volume=VolumeObservation(observed=Decimal("1"), expected=Decimal("1")),
            price=PriceObservation(session_return=Decimal("0")),
            triggers=(),
        )


def test_a_volume_shock_cannot_fire_without_an_expected_volume() -> None:
    """Otherwise the event asserts a comparison against a number that was never established."""
    with pytest.raises(ValueError, match="expected volume"):
        MarketAnomalyEvent(
            symbol="SPY",
            session_date=date(2026, 3, 2),
            segment=SessionSegment.FULL_SESSION,
            observed_at=NOW,
            detector_identity="anomaly-detector-v1-x",
            feed=MarketDataFeed.SIP,
            volume_coverage=VolumeCoverage.CONSOLIDATED,
            cadence=config().cadence,
            baseline_sessions=(date(2026, 3, 1),),
            volume=VolumeObservation(observed=Decimal("1"), expected=None),
            price=PriceObservation(session_return=Decimal("0")),
            triggers=(TriggerReason.VOLUME_SHOCK,),
        )


def test_a_quiet_session_produces_nothing() -> None:
    """The stream must be mostly empty, or the routing premise fails on arithmetic alone."""
    history = sessions([*quiet(), 1_003_000])

    assert detect(symbol="SPY", history=history, config=config(), observed_at=NOW) is None
