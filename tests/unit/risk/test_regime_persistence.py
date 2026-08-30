"""Regime context made durable, and bound without redefining what it describes (#59).

The binding shape is the point. #94 established that a versioned reading of a session must not
be folded into the observation it describes: re-running the definition legitimately changes the
reading while changing nothing about what happened, and an identity that moved with it would let
a redefinition rewrite history.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.anomaly import AnomalyDetectorConfig, SessionObservation, detect
from quantbot.risk.regime import (
    BreadthState,
    MarketRegimeSnapshot,
    TrendState,
    VolatilityState,
)
from quantbot.storage import Database, StorageRepository

AS_OF = date(2026, 3, 20)


def snapshot(**overrides: object) -> MarketRegimeSnapshot:
    fields: dict[str, object] = {
        "as_of_session": AS_OF,
        "benchmark_symbol": "SPY",
        "source": "alpaca:sip:all:1day",
        "trend": TrendState.ABOVE,
        "volatility": VolatilityState.NORMAL,
        "breadth": BreadthState.BROAD,
        "realised_volatility": Decimal("0.13"),
        "participation": Decimal("0.6"),
    }
    fields.update(overrides)
    return MarketRegimeSnapshot(**fields)  # type: ignore[arg-type]


def anomaly_event():
    history = [
        SessionObservation(
            session_date=date(2026, 1, 5) + timedelta(days=index),
            volume=Decimal(1_000_000 + (index % 7) * 1_000),
            close=Decimal(str(100.0 + (index % 5) * 0.1)),
        )
        for index in range(60)
    ]
    history.append(
        SessionObservation(session_date=AS_OF, volume=Decimal(40_000_000), close=Decimal("100.2"))
    )
    built = detect(
        symbol="SPY",
        history=history,
        config=AnomalyDetectorConfig(),
        observed_at=datetime(2026, 3, 20, 20, 5, tzinfo=UTC),
    )
    assert built is not None
    return built


def test_a_snapshot_persists_once_however_often_it_is_re_derived(tmp_path: Path) -> None:
    """The same session under the same method is one reading, not many."""
    database = Database.create(tmp_path / "regime.db")
    reading = snapshot()

    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.record_market_regime_snapshot(reading.as_row()) is True
        assert repository.record_market_regime_snapshot(reading.as_row()) is False
        stored = repository.list_market_regime_snapshots()
    database.close()

    assert len(stored) == 1
    assert stored[0]["snapshot_id"] == reading.identity
    assert stored[0]["trend"] == "ABOVE"
    assert stored[0]["method_version"] == 1


def test_a_second_definition_adds_a_reading_without_touching_the_first(tmp_path: Path) -> None:
    """Revising the method mints a new snapshot; it never relabels the old one."""
    database = Database.create(tmp_path / "regime.db")
    first = snapshot()
    revised = snapshot(method_version=2)

    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.record_market_regime_snapshot(first.as_row())
        assert repository.record_market_regime_snapshot(revised.as_row()) is True
        stored = repository.list_market_regime_snapshots(as_of_session=AS_OF.isoformat())
    database.close()

    assert len(stored) == 2
    assert {row["method_version"] for row in stored} == {1, 2}
    assert first.identity != revised.identity


def test_an_observation_can_carry_two_regime_readings_of_its_session(tmp_path: Path) -> None:
    """The #94 binding shape: a relation, so a second definition adds a row.

    Folding the reading into the event would mean re-running the regime definition redefines
    what was observed -- and, if the identity did not move with it, two different payloads would
    share one content-addressed id.
    """
    database = Database.create(tmp_path / "regime.db")
    event = anomaly_event()
    first = snapshot()
    revised = snapshot(method_version=2, volatility=VolatilityState.STRESSED)

    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.record_anomaly_event(event.as_row())
        repository.record_market_regime_snapshot(first.as_row())
        repository.record_market_regime_snapshot(revised.as_row())
        assert repository.bind_anomaly_regime(
            anomaly_event_id=event.event_id, snapshot_id=first.identity
        ) is True
        assert repository.bind_anomaly_regime(
            anomaly_event_id=event.event_id, snapshot_id=first.identity
        ) is False
        assert repository.bind_anomaly_regime(
            anomaly_event_id=event.event_id, snapshot_id=revised.identity
        ) is True
        contexts = repository.list_anomaly_regime_contexts(anomaly_event_id=event.event_id)
        events = repository.list_anomaly_events()
    database.close()

    assert len(contexts) == 2
    assert len(events) == 1, "a second regime reading must not create a second observation"
    assert events[0]["event_id"] == event.event_id


def test_a_binding_cannot_reference_a_snapshot_that_was_never_stored(tmp_path: Path) -> None:
    """A reference to an unrecorded reading is a claim with no subject."""
    database = Database.create(tmp_path / "regime.db")
    event = anomaly_event()

    with database.transaction() as session:
        StorageRepository(session).record_anomaly_event(event.as_row())

    with pytest.raises(Exception, match="FOREIGN KEY|foreign key"):
        with database.transaction() as session:
            StorageRepository(session).bind_anomaly_regime(
                anomaly_event_id=event.event_id,
                snapshot_id="market-regime-v1-doesnotexist",
            )
    database.close()


def test_an_unavailable_axis_survives_the_round_trip_with_its_reason(tmp_path: Path) -> None:
    """A gap that lost its explanation in storage is a gap that reads as a measurement."""
    database = Database.create(tmp_path / "regime.db")
    degraded = snapshot(
        volatility=VolatilityState.UNAVAILABLE,
        realised_volatility=None,
        unavailable=("volatility: 30 benchmark sessions available and 250 required",),
    )

    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.record_market_regime_snapshot(degraded.as_row())
        stored = repository.list_market_regime_snapshots()
    database.close()

    assert stored[0]["volatility"] == "UNAVAILABLE"
    assert stored[0]["realised_volatility"] is None
    assert "250 required" in str(stored[0]["unavailable"])


def test_the_migration_is_additive_and_leaves_prior_rows_alone(tmp_path: Path) -> None:
    """A real 0015 store through the real migration.

    Asserted against `SCHEMA_VERSION` rather than a literal. An earlier version hardcoded 16 and
    broke the moment 0017 landed -- a test that has to be edited by every later migration is a
    test that will eventually be edited without being read.
    """
    from alembic import command
    from alembic.config import Config

    path = tmp_path / "v15.db"
    config = Config()
    config.set_main_option(
        "script_location", str(Path("src/quantbot/storage/migrations").resolve())
    )
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path.as_posix()}")
    command.upgrade(config, "0015")

    database = Database.migrate(path)
    try:
        with database.transaction() as session:
            assert StorageRepository(session).list_market_regime_snapshots() == []
        with database.engine.connect() as connection:
            marker = connection.exec_driver_sql("SELECT version FROM schema_version").fetchone()
    finally:
        database.close()

    from quantbot.storage.schema import SCHEMA_VERSION

    assert marker is not None and marker[0] == SCHEMA_VERSION
