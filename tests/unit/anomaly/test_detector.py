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
    FEED_COVERAGE,
    AnomalyDependenceContext,
    AnomalyDetectorConfig,
    DataQuality,
    MarketAnomalyEvent,
    PriceObservation,
    SessionObservation,
    SessionSegment,
    TriggerReason,
    VolumeCoverage,
    VolumeObservation,
    dependence_contexts,
    detect,
    refusal,
    supports_price_trigger,
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


def drifting(count: int = 60, *, jump: float | None = None) -> list[float]:
    """Prices with small dispersion, optionally ending in a large move.

    A flat baseline has zero dispersion, so the price branch refuses it -- correctly, and that
    is tested elsewhere. These fixtures need it to vary so a sigma exists to exceed.
    """
    prices = [100.0 + (index % 5) * 0.1 for index in range(count)]
    return [*prices, jump] if jump is not None else prices


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
            family_quality={TriggerReason.VOLUME_SHOCK: DataQuality.OK},
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

    contexts = dependence_contexts(
        events,
        {"SPY": "cluster-a", "QQQ": "cluster-a"},
        snapshot_identity="dependence-v1-abcd",
    )
    by_event = {context.anomaly_event_id: context for context in contexts}
    by_symbol = {event.symbol: by_event[event.event_id] for event in events}

    assert by_symbol["SPY"].group_key == "cluster-a"
    assert by_symbol["QQQ"].group_key == "cluster-a"
    assert by_symbol["TLT"].group_key is None, (
        "a symbol the dependence snapshot could not measure must record no group rather than "
        "being defaulted into one of its own"
    )
    # A row exists for every event, including the ungrouped one: its presence is what separates
    # "this matrix had nothing for it" from "no matrix ran".
    assert len(contexts) == len(events)
    for context in contexts:
        assert context.dependence_snapshot_identity == "dependence-v1-abcd"


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
    assert event.family_quality[TriggerReason.VOLUME_SHOCK] is DataQuality.OK
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
            family_quality={},
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
            family_quality={TriggerReason.VOLUME_SHOCK: DataQuality.OK},
        )


def test_a_quiet_session_produces_nothing() -> None:
    """The stream must be mostly empty, or the routing premise fails on arithmetic alone."""
    history = sessions([*quiet(), 1_003_000])

    assert detect(symbol="SPY", history=history, config=config(), observed_at=NOW) is None


# --- trigger families (#56 follow-up, Sol item 1) ------------------------------------------------


def test_a_venue_feed_still_emits_price_events_and_only_price_events() -> None:
    """The regression Sol asked for, and the reason it is not simply "IEX refuses everything".

    The two families do not share a fate, and the measurement says so. On the same corpus a
    `PRICE_VELOCITY` trigger computed independently on each feed scores **96.3% precision and
    99.0% recall**, with a median absolute close difference of 0.015%, because arbitrage pins
    every venue's last print to every other. Volume is the opposite: a venue reports a fraction
    of it, so IEX scored 36.6% / 22.8%.

    Ordinary volume, extreme price, IEX feed. Exactly one family may fire.
    """
    history = sessions([*quiet(), 1_002_000], closes=drifting(jump=130.0))

    event = detect(
        symbol="SPY", history=history, config=config(feed=MarketDataFeed.IEX), observed_at=NOW
    )

    assert event is not None, "a venue feed must still be able to report a price anomaly"
    assert event.triggers == (TriggerReason.PRICE_VELOCITY,)
    assert TriggerReason.VOLUME_SHOCK not in event.triggers
    assert event.volume_coverage is VolumeCoverage.SINGLE_VENUE

    assert refusal(config(feed=MarketDataFeed.IEX), history, TriggerReason.PRICE_VELOCITY) is None
    assert refusal(config(feed=MarketDataFeed.IEX), history, TriggerReason.VOLUME_SHOCK) is not None


def test_a_huge_volume_spike_on_a_venue_feed_stays_silent() -> None:
    """The other half of the same guard: no amount of IEX volume may manufacture an event."""
    history = sessions([*quiet(), 500_000_000])

    assert detect(
        symbol="SPY", history=history, config=config(feed=MarketDataFeed.IEX), observed_at=NOW
    ) is None


def test_disabling_a_family_silences_it_on_a_feed_that_could_carry_it() -> None:
    """Enabling is intent and capability is permission; both are required, neither suffices."""
    history = sessions([*quiet(), 40_000_000], closes=drifting(jump=130.0))

    both = detect(symbol="SPY", history=history, config=config(), observed_at=NOW)
    assert both is not None
    assert set(both.triggers) == {TriggerReason.VOLUME_SHOCK, TriggerReason.PRICE_VELOCITY}

    volume_only = detect(
        symbol="SPY",
        history=history,
        config=config(enabled_triggers=frozenset({TriggerReason.VOLUME_SHOCK})),
        observed_at=NOW,
    )
    assert volume_only is not None
    assert volume_only.triggers == (TriggerReason.VOLUME_SHOCK,)


def test_the_enabled_families_are_part_of_the_detector_identity() -> None:
    """Which questions a detector may ask changes its stream as much as any threshold does."""
    base = config()
    narrowed = config(enabled_triggers=frozenset({TriggerReason.VOLUME_SHOCK}))

    assert base.identity != narrowed.identity
    # A set serialises in arbitrary order; two identical detectors must still agree.
    reordered = config(
        enabled_triggers=frozenset({TriggerReason.PRICE_VELOCITY, TriggerReason.VOLUME_SHOCK})
    )
    assert reordered.identity == base.identity


def test_a_family_with_no_detector_behind_it_cannot_be_enabled() -> None:
    """`RANGE_EXPANSION` is declared in #56 and not yet computed.

    Same rule as the risk layer's unimplemented-veto set: a declared-but-absent control reads as
    protection that is not there, which is what #75 existed to remove.
    """
    with pytest.raises(ValueError, match="no detector behind it"):
        config(enabled_triggers=frozenset({TriggerReason.RANGE_EXPANSION}))
    with pytest.raises(ValueError, match="never emit an event"):
        config(enabled_triggers=frozenset())


# --- group binding (#56 follow-up, Sol item 2) ---------------------------------------------------


def test_a_group_cannot_be_claimed_without_naming_the_snapshot_that_produced_it() -> None:
    """An unbound cluster is an ungrounded claim about what moves together.

    Without this, a caller could stamp clusters computed from one dependence matrix onto events
    naming a different one -- or naming none -- and nothing downstream could detect the mismatch.
    The grouping helper takes the identity as a required argument for the same reason.
    """
    with pytest.raises(ValueError):
        AnomalyDependenceContext(
            anomaly_event_id="anomaly-2026-03-02-abcd",
            dependence_snapshot_identity="",
            group_key="cluster-a",
        )

    history = sessions([*quiet(), 40_000_000])
    event = detect(symbol="SPY", history=history, config=config(), observed_at=NOW)
    assert event is not None
    with pytest.raises(ValueError, match="requires the identity"):
        dependence_contexts([event], {"SPY": "cluster-a"}, snapshot_identity="   ")


def test_every_feed_the_enum_offers_has_been_characterised() -> None:
    """The guard that makes the fail-closed default meaningful rather than decorative.

    `supports_price_trigger` refuses an `UNKNOWN` feed, but every member of `MarketDataFeed` is
    currently characterised, so that branch cannot be reached today -- dropping the check leaves
    the suite green, which by this project's own rule means it is not yet a protection.

    This is what makes it one. Adding a feed to the enum without saying what its volume covers
    fails here, and until then the refusal branch is a live default rather than dead code.
    """
    uncharacterised = [feed for feed in MarketDataFeed if feed not in FEED_COVERAGE]

    assert not uncharacterised, (
        f"{[f.value for f in uncharacterised]} has no declared volume coverage, so the detector "
        "would have to guess whether its prints sample the market or report it"
    )


def test_an_uncharacterised_feed_refuses_both_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal branch itself, exercised by simulating the feed nobody has measured yet.

    Guessing is the permissive direction: a new venue feed assumed consolidated would reproduce
    exactly the IEX defect this module was written to stop.
    """
    monkeypatch.delitem(FEED_COVERAGE, MarketDataFeed.IEX)

    assert volume_coverage(MarketDataFeed.IEX) is VolumeCoverage.UNKNOWN
    assert supports_volume_trigger(MarketDataFeed.IEX) is False
    assert supports_price_trigger(MarketDataFeed.IEX) is False

    history = sessions([*quiet(), 40_000_000], closes=drifting(jump=130.0))
    assert detect(
        symbol="SPY", history=history, config=config(feed=MarketDataFeed.IEX), observed_at=NOW
    ) is None

    reason = refusal(config(feed=MarketDataFeed.IEX), history, TriggerReason.PRICE_VELOCITY)
    assert reason is not None and "uncharacterised" in reason


# --- identity boundary (#56 follow-up: raw observation vs dependence context) --------------------


def test_grouping_cannot_change_what_a_raw_event_id_describes() -> None:
    """The defect this split exists to remove, pinned as a property.

    Grouping used to be stamped onto the event while `event_id` excluded it, so the grouped copy
    carried a different `group_key` under the *same* identity. Two materially different immutable
    payloads shared one supposedly content-addressed id, and a `market_anomaly_events` table
    keyed on it would have deduplicated them into whichever arrived first.

    The observation and the grouping answer different questions on different clocks: what
    happened is fixed when measured, while one matrix's opinion about what moved with it changes
    whenever that matrix is recomputed. So the event has no grouping fields at all -- the
    strongest available form of "grouping cannot redefine it".
    """
    history = sessions([*quiet(), 40_000_000])
    event = detect(symbol="SPY", history=history, config=config(), observed_at=NOW)
    assert event is not None

    assert not hasattr(event, "group_key")
    assert not hasattr(event, "dependence_snapshot_identity")

    first = dependence_contexts([event], {"SPY": "a"}, snapshot_identity="dependence-v1-one")
    second = dependence_contexts([event], {"SPY": "z"}, snapshot_identity="dependence-v1-two")

    # The observation is untouched by either matrix.
    assert first[0].anomaly_event_id == event.event_id
    assert second[0].anomaly_event_id == event.event_id
    # And the two opinions are distinguishable from each other.
    assert first[0].identity != second[0].identity


def test_a_dependence_context_is_addressed_by_everything_that_defines_it() -> None:
    """Keyed on the raw event and the exact matrix, so re-running one is a new relation."""
    base = AnomalyDependenceContext(
        anomaly_event_id="anomaly-2026-03-20-abcd",
        dependence_snapshot_identity="dependence-v1-one",
        group_key="cluster-a",
    )

    assert base.identity.startswith("anomaly-dependence-v1-")
    for change in (
        {"anomaly_event_id": "anomaly-2026-03-20-ffff"},
        {"dependence_snapshot_identity": "dependence-v1-two"},
        {"group_key": "cluster-b"},
        {"group_key": None},
        {"method_version": 2},
    ):
        assert base.model_copy(update=change).identity != base.identity, change


# --- family-specific data quality ---------------------------------------------------------------


def test_a_valid_price_event_is_not_condemned_by_the_volume_family_it_shares_a_feed_with() -> None:
    """One verdict for the whole event was wrong in a specific, costly way.

    On IEX the volume family cannot run, and a scalar `FEED_UNFIT` then described a perfectly
    valid price observation as unusable -- so downstream routing would discard a real event for
    a reason that applies only to a question it was not asking.
    """
    history = sessions([*quiet(), 1_002_000], closes=drifting(jump=130.0))
    event = detect(
        symbol="SPY", history=history, config=config(feed=MarketDataFeed.IEX), observed_at=NOW
    )

    assert event is not None
    assert event.triggers == (TriggerReason.PRICE_VELOCITY,)
    assert event.family_quality[TriggerReason.PRICE_VELOCITY] is DataQuality.OK
    assert event.family_quality[TriggerReason.VOLUME_SHOCK] is DataQuality.FEED_UNFIT

    # The row a store would keep must carry both verdicts, not a collapsed one.
    assert event.as_row()["family_quality"] == "PRICE_VELOCITY=OK,VOLUME_SHOCK=FEED_UNFIT"


def test_a_family_cannot_fire_while_recorded_as_unmeasurable() -> None:
    """Otherwise the event asserts a trigger and its own inability to have computed it."""
    with pytest.raises(ValueError, match="fired while recorded as unmeasurable"):
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
            triggers=(TriggerReason.VOLUME_SHOCK,),
            family_quality={TriggerReason.VOLUME_SHOCK: DataQuality.FEED_UNFIT},
        )
