"""Durable, replayable anomaly observations and dependence contexts (#56)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.anomaly import (
    AnomalyDependenceContext,
    DataQuality,
    MarketAnomalyEvent,
    PriceObservation,
    SessionSegment,
    TriggerReason,
    VolumeCoverage,
    VolumeObservation,
)
from quantbot.market_data import MarketDataFeed
from quantbot.risk.freshness import DataCadence
from quantbot.storage import Database, StateConflictError, StorageRepository

NOW = datetime(2026, 3, 9, 21, 5, tzinfo=UTC)
BASELINE = (date(2026, 3, 2), date(2026, 3, 4), date(2026, 3, 6))


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    store = Database.create(tmp_path / "anomalies.db")
    yield store
    store.close()


def anomaly_event(*, observed_at: datetime = NOW) -> MarketAnomalyEvent:
    return MarketAnomalyEvent(
        symbol="SPY",
        session_date=date(2026, 3, 9),
        segment=SessionSegment.CLOSING,
        observed_at=observed_at,
        detector_identity="anomaly-detector-v3-0123456789abcdef",
        feed=MarketDataFeed.SIP,
        volume_coverage=VolumeCoverage.CONSOLIDATED,
        cadence=DataCadence.DAILY_BAR,
        baseline_sessions=BASELINE,
        volume=VolumeObservation(
            observed=Decimal("40000000.125"),
            expected=Decimal("10000000.500"),
            robust_z=Decimal("7.1250"),
        ),
        price=PriceObservation(
            session_return=Decimal("0.031250"),
            trailing_sigma=Decimal("0.007500"),
            range_fraction=Decimal("0.043210"),
            expected_range_fraction=Decimal("0.012340"),
        ),
        triggers=(TriggerReason.PRICE_VELOCITY, TriggerReason.VOLUME_SHOCK),
        family_quality={
            TriggerReason.PRICE_VELOCITY: DataQuality.OK,
            TriggerReason.VOLUME_SHOCK: DataQuality.OK,
        },
    )


def dependence(
    event: MarketAnomalyEvent,
    *,
    snapshot: str = "dependence-v1-window-a",
    group: str | None = "cluster-index",
) -> AnomalyDependenceContext:
    return AnomalyDependenceContext(
        method_version=2,
        anomaly_event_id=event.event_id,
        dependence_snapshot_identity=snapshot,
        group_key=group,
    )


def test_market_anomaly_event_round_trips_every_field_and_exact_baseline(
    database: Database,
) -> None:
    event = anomaly_event()

    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.record_market_anomaly_event(event.as_row()) is True
        assert repository.record_market_anomaly_event(event.as_row()) is False

        stored = repository.get_market_anomaly_event(event.event_id)
        assert stored is not None
        restored = MarketAnomalyEvent.from_row(stored)

        assert restored == event
        assert restored.event_id == event.event_id
        assert restored.baseline_sessions == BASELINE
        assert restored.price.range_fraction == Decimal("0.043210")
        assert restored.price.expected_range_fraction == Decimal("0.012340")
        assert repository.list_market_anomaly_events(symbol="SPY") == [stored]


def test_a_replay_clock_is_idempotent_and_does_not_overwrite_first_observation_time(
    database: Database,
) -> None:
    original = anomaly_event()
    replay = anomaly_event(observed_at=NOW + timedelta(minutes=7))
    assert replay.event_id == original.event_id

    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.record_market_anomaly_event(original.as_row()) is True
        assert repository.record_market_anomaly_event(replay.as_row()) is False

        stored = repository.get_market_anomaly_event(original.event_id)
        assert stored is not None
        assert MarketAnomalyEvent.from_row(stored).observed_at == original.observed_at


def test_same_event_id_with_different_evidence_conflicts_and_keeps_original(
    database: Database,
) -> None:
    event = anomaly_event()
    conflicting = dict(event.as_row())
    conflicting["detector_identity"] = "anomaly-detector-v99-tampered"

    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.record_market_anomaly_event(event.as_row())

        with pytest.raises(StateConflictError, match="anomaly event"):
            repository.record_market_anomaly_event(conflicting)

        stored = repository.get_market_anomaly_event(event.event_id)
        assert stored is not None
        assert MarketAnomalyEvent.from_row(stored) == event


def test_dependence_contexts_round_trip_and_allow_distinct_frozen_snapshots(
    database: Database,
) -> None:
    event = anomaly_event()
    first = dependence(event)
    second = dependence(event, snapshot="dependence-v1-window-b", group=None)

    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.record_market_anomaly_event(event.as_row())

        assert repository.record_anomaly_dependence_context(first.as_row()) is True
        assert repository.record_anomaly_dependence_context(first.as_row()) is False
        assert repository.record_anomaly_dependence_context(second.as_row()) is True

        stored = repository.get_anomaly_dependence_context(first.identity)
        assert stored is not None
        assert AnomalyDependenceContext.from_row(stored) == first
        assert [
            AnomalyDependenceContext.from_row(row)
            for row in repository.list_anomaly_dependence_contexts(
                anomaly_event_id=event.event_id
            )
        ] == [first, second]


def test_one_snapshot_cannot_assign_two_groups_to_the_same_event_and_method(
    database: Database,
) -> None:
    event = anomaly_event()
    first = dependence(event, group="cluster-index")
    contradiction = dependence(event, group="cluster-other")
    assert contradiction.identity != first.identity

    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.record_market_anomaly_event(event.as_row())
        repository.record_anomaly_dependence_context(first.as_row())

        with pytest.raises(StateConflictError, match="dependence context"):
            repository.record_anomaly_dependence_context(contradiction.as_row())

        assert repository.list_anomaly_dependence_contexts(
            anomaly_event_id=event.event_id
        ) == [first.as_row()]


def test_a_dependence_context_refuses_an_unknown_event(database: Database) -> None:
    orphan = AnomalyDependenceContext(
        method_version=1,
        anomaly_event_id="anomaly-2026-03-09-absent",
        dependence_snapshot_identity="dependence-v1-window-a",
        group_key=None,
    )

    with database.transaction() as session:
        with pytest.raises(StateConflictError, match="unknown anomaly event"):
            StorageRepository(session).record_anomaly_dependence_context(orphan.as_row())


def test_round_trip_helpers_reject_corrupt_storage_projections() -> None:
    event = anomaly_event()
    corrupt_event = dict(event.as_row())
    corrupt_event["baseline_session_count"] = 99
    with pytest.raises(ValueError, match="stored anomaly event"):
        MarketAnomalyEvent.from_row(corrupt_event)

    context = dependence(event)
    corrupt_context = dict(context.as_row())
    corrupt_context["context_id"] = "anomaly-dependence-v2-tampered"
    with pytest.raises(ValueError, match="stored anomaly dependence context"):
        AnomalyDependenceContext.from_row(corrupt_context)
