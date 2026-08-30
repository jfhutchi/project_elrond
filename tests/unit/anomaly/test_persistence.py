"""Durable anomaly observations, and the ways a replay becomes a second sample (#56).

Every test here goes through the real schema and repository rather than a fixture that agrees
with the model. The premise of #56 is that expensive research follows information density, and
that premise is only measurable if one piece of information is stored once -- a detector that
counts a replayed session twice inflates exactly the number the routing experiment reports.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.anomaly import (
    AnomalyDependenceContext,
    AnomalyDetectorConfig,
    SessionObservation,
    dependence_contexts,
    detect,
)
from quantbot.storage import Database, StorageRepository
from quantbot.storage.repositories import StateConflictError

NOW = datetime(2026, 3, 20, 20, 5, tzinfo=UTC)


def history(*, volume: int = 40_000_000, close: str = "100.2") -> list[SessionObservation]:
    sessions = [
        SessionObservation(
            session_date=date(2026, 1, 5) + timedelta(days=index),
            volume=Decimal(1_000_000 + (index % 7) * 1_000),
            close=Decimal(str(100.0 + (index % 5) * 0.1)),
        )
        for index in range(60)
    ]
    sessions.append(
        SessionObservation(
            session_date=date(2026, 3, 20), volume=Decimal(volume), close=Decimal(close)
        )
    )
    return sessions


def event(**overrides: object):
    built = detect(
        symbol=str(overrides.get("symbol", "SPY")),
        history=history(),
        config=AnomalyDetectorConfig(),
        observed_at=NOW,
    )
    assert built is not None
    return built


def store(tmp_path: Path) -> Database:
    return Database.create(tmp_path / "anomaly.db")


def test_a_replayed_session_is_stored_once_however_often_it_is_re_derived(
    tmp_path: Path,
) -> None:
    """The property the routing experiment depends on arithmetically.

    `event_id` excludes `observed_at`, so a retry, a restarted worker or a backfill re-deriving
    the same session produces the same id. Counting those as separate observations would inflate
    the event rate this subsystem is being judged on.
    """
    database = store(tmp_path)
    first = event()
    replayed = detect(
        symbol="SPY",
        history=history(),
        config=AnomalyDetectorConfig(),
        observed_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert replayed is not None
    assert replayed.observed_at != first.observed_at

    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.record_anomaly_event(first.as_row()) is True
        assert repository.record_anomaly_event(first.as_row()) is False
        assert repository.record_anomaly_event(replayed.as_row()) is False, (
            "a different clock must not make one observation into two"
        )
        assert len(repository.list_anomaly_events()) == 1
    database.close()


def test_two_different_observations_cannot_share_an_id(tmp_path: Path) -> None:
    """Where the raw/context identity defect would resurface if it ever returned.

    A repeat that differs is not a replay. Accepting it silently would let the store hold one
    row while two different things happened, which is the failure the split was made to prevent.
    """
    database = store(tmp_path)
    original = event()
    forged = dict(original.as_row())
    forged["observed_volume"] = "999999999"

    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.record_anomaly_event(original.as_row()) is True
        with pytest.raises(StateConflictError, match="different observations"):
            repository.record_anomaly_event(forged)
    database.close()


def test_a_second_matrix_adds_an_opinion_without_touching_the_observation(
    tmp_path: Path,
) -> None:
    """One observation, many dependence contexts -- the durable form of the #94 split.

    Re-running the correlation matrix over a different window legitimately changes which symbols
    were moving together. That must produce a new relation, never a rewrite of what happened.
    """
    database = store(tmp_path)
    observed = event()
    first = dependence_contexts([observed], {"SPY": "a"}, snapshot_identity="dependence-v1-one")
    second = dependence_contexts([observed], {"SPY": "z"}, snapshot_identity="dependence-v1-two")

    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.record_anomaly_event(observed.as_row())
        assert repository.record_anomaly_dependence_context(first[0].as_row()) is True
        assert repository.record_anomaly_dependence_context(first[0].as_row()) is False
        assert repository.record_anomaly_dependence_context(second[0].as_row()) is True

        contexts = repository.list_anomaly_dependence_contexts(
            anomaly_event_id=observed.event_id
        )
        stored = repository.list_anomaly_events()

    assert len(contexts) == 2
    assert {row["dependence_snapshot_identity"] for row in contexts} == {
        "dependence-v1-one",
        "dependence-v1-two",
    }
    assert len(stored) == 1, "a second matrix must not create a second observation"
    database.close()


def test_one_matrix_cannot_hold_two_opinions_about_one_observation(tmp_path: Path) -> None:
    """Enforced by the store, not by the caller remembering to check.

    Two different groupings from the *same* snapshot would mean the frozen matrix disagreed with
    itself, so the constraint lives in the schema where a buggy writer cannot route around it.
    """
    database = store(tmp_path)
    observed = event()
    one = AnomalyDependenceContext(
        anomaly_event_id=observed.event_id,
        dependence_snapshot_identity="dependence-v1-one",
        group_key="a",
    )
    contradiction = AnomalyDependenceContext(
        anomaly_event_id=observed.event_id,
        dependence_snapshot_identity="dependence-v1-one",
        group_key="z",
    )
    assert one.identity != contradiction.identity, "distinct rows, so only the DB can refuse"

    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.record_anomaly_event(observed.as_row())
        repository.record_anomaly_dependence_context(one.as_row())

    with pytest.raises(Exception, match="one_context_per_event_and_snapshot|UNIQUE"):
        with database.transaction() as session:
            StorageRepository(session).record_anomaly_dependence_context(
                contradiction.as_row()
            )
    database.close()


def test_a_context_cannot_reference_an_observation_that_was_never_stored(
    tmp_path: Path,
) -> None:
    """A grouping of nothing is a claim with no subject."""
    database = store(tmp_path)
    orphan = AnomalyDependenceContext(
        anomaly_event_id="anomaly-2026-03-20-doesnotexist",
        dependence_snapshot_identity="dependence-v1-one",
        group_key="a",
    )

    with pytest.raises(Exception, match="FOREIGN KEY|foreign key"):
        with database.transaction() as session:
            StorageRepository(session).record_anomaly_dependence_context(orphan.as_row())
    database.close()


def test_the_stored_row_carries_the_provenance_a_later_reader_needs(tmp_path: Path) -> None:
    """Feed semantics, detector identity and the exact baseline window, on the row itself.

    Without the feed on the row, a venue feed's volume can be read downstream as full-market
    volume -- the defect this subsystem was built around. Without the baseline window, "the same
    sample" is a claim rather than a property, which is what #84 corrected for the dependence
    snapshot.
    """
    database = store(tmp_path)
    observed = event()

    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.record_anomaly_event(observed.as_row())
        row = repository.list_anomaly_events()[0]
    database.close()

    assert row["event_id"] == observed.event_id
    assert row["feed"] == observed.feed.value
    assert row["volume_coverage"] == observed.volume_coverage.value
    assert row["cadence"] == observed.cadence.value
    assert row["detector_identity"] == observed.detector_identity
    assert row["baseline_session_count"] == len(observed.baseline_sessions)
    assert row["baseline_first_session"] == observed.baseline_sessions[0].isoformat()
    assert row["baseline_last_session"] == observed.baseline_sessions[-1].isoformat()
    assert "VOLUME_SHOCK" in str(row["triggers"])
    assert "VOLUME_SHOCK=OK" in str(row["family_quality"])


def test_the_migration_is_additive_and_leaves_prior_rows_alone(tmp_path: Path) -> None:
    """A real 0013 store with a real row in it, upgraded through the real migration."""
    from alembic import command
    from alembic.config import Config

    path = tmp_path / "v13.db"
    config = Config()
    config.set_main_option(
        "script_location", str(Path("src/quantbot/storage/migrations").resolve())
    )
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path.as_posix()}")
    command.upgrade(config, "0013")

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO rejected_candidates (observation_id, candidate_id, strategy_id, "
            "configuration_hash, symbol, direction, observed_at, session_date, "
            "reference_price, state, unavailable_reason) VALUES ('obs-1', 'cand-1', "
            "'adaptive-momentum', 'cfg', 'SPY', 'LONG', '2026-03-20T20:05:00Z', '2026-03-20', "
            "'500', 'COUNTERFACTUAL_UNAVAILABLE', 'horizon has not elapsed')"
        )
        connection.commit()
    finally:
        connection.close()

    database = Database.migrate(path)
    try:
        with database.transaction() as session:
            repository = StorageRepository(session)
            assert repository.list_anomaly_events() == []
            survivors = repository.list_rejected_candidates()
    finally:
        database.close()

    assert len(survivors) == 1, "the migration must not disturb what was already recorded"
