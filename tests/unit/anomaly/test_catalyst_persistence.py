"""Catalyst assessments made durable, with the confusion enforced by the database (#56).

`CatalystAssessment.as_row()` was written for a table that did not exist and nothing ever called
it -- the seventh instance in this project of a control correct in isolation and never reached in
production. This is the table, and the constraints that stop the one mistake this whole design
exists to prevent from reaching disk even if the model that writes it is changed.

The model already refuses to construct a `NO_PUBLIC_CATALYST_FOUND` beside an unhealthy source.
The check constraints below are not redundant with that: a migration, a repair script, or a
future writer that bypasses the model can all put a row in this table, and "no public catalyst"
sitting beside a rate-limited provider is a manufactured discovery whichever code wrote it.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from quantbot.anomaly.catalyst import (
    CatalystCandidate,
    CatalystOutcome,
    SourceHealth,
    SourceResult,
    assess_catalysts,
)
from quantbot.storage import Database, StorageRepository
from tests.unit.anomaly.test_persistence import NOW as EVENT_OBSERVED
from tests.unit.anomaly.test_persistence import event as anomaly_event

OBSERVED = EVENT_OBSERVED
#: The real detector's id for the fixture event, so the foreign key is exercised rather than
#: satisfied by a string somebody invented.
EVENT = anomaly_event().event_id


@pytest.fixture
def database(tmp_path: Path):
    db = Database.create(tmp_path / "catalyst.db")
    try:
        yield db
    finally:
        db.close()


def candidate(**overrides: object) -> CatalystCandidate:
    fields: dict[str, object] = {
        "source": "benzinga",
        "external_id": "12345",
        "published_at": OBSERVED - timedelta(hours=6),
        "retrieved_at": OBSERVED,
        "headline": "Company announces something",
    }
    fields.update(overrides)
    return CatalystCandidate(**fields)  # type: ignore[arg-type]


def healthy(*candidates: CatalystCandidate) -> SourceResult:
    return SourceResult(
        source="alpaca-news", health=SourceHealth.HEALTHY, candidates=candidates
    )


def assessment(*sources: SourceResult):
    return assess_catalysts(
        anomaly_event_id=EVENT, observed_at=OBSERVED, sources=list(sources)
    )


def seed_event(database: Database) -> None:
    """The anomaly the assessment hangs off, recorded through the real path.

    Written by the detector and the repository rather than by hand-rolled SQL: a seed that
    invents its own columns drifts from the table it is meant to reference, and then the foreign
    key under test is satisfied by a row production could never produce.
    """
    with database.transaction() as session:
        StorageRepository(session).record_anomaly_event(anomaly_event().as_row())


def store(database: Database, row: dict[str, object]) -> None:
    with database.transaction() as session:
        session.connection().exec_driver_sql(
            "INSERT INTO catalyst_assessments (assessment_id, anomaly_event_id, observed_at, "
            "lookback_seconds, outcome, sources_consulted, unhealthy_sources, catalyst_ids, "
            "published_after_event, unplaceable, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            tuple(row.values()),
        )


def row(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "assessment_id": "catalyst-v1-handwritten",
        "anomaly_event_id": EVENT,
        "observed_at": OBSERVED.isoformat(),
        "lookback_seconds": 3600,
        "outcome": "NO_PUBLIC_CATALYST_FOUND",
        "sources_consulted": "alpaca-news",
        "unhealthy_sources": "",
        "catalyst_ids": "",
        "published_after_event": 0,
        "unplaceable": 0,
        "detail": "written directly, bypassing the model",
    }
    fields.update(overrides)
    return fields


# --- the round trip ---------------------------------------------------------------------------


def test_a_finding_round_trips_with_the_sources_that_produced_it(database: Database) -> None:
    """The denominator is the part a later reader most needs and least able to reconstruct."""
    seed_event(database)
    found = assessment(healthy(candidate()))

    with database.transaction() as session:
        assert StorageRepository(session).record_catalyst_assessment(found.as_row()) is True

    with database.transaction() as session:
        stored = StorageRepository(session).list_catalyst_assessments(anomaly_event_id=EVENT)

    assert len(stored) == 1
    assert stored[0]["outcome"] == "CATALYST_FOUND"
    assert stored[0]["catalyst_ids"] == "12345"
    assert stored[0]["sources_consulted"] == "alpaca-news"
    assert stored[0]["unhealthy_sources"] == ""


def test_recording_the_same_lookup_twice_stores_once(database: Database) -> None:
    """A retried enrichment must not inflate how often an anomaly appears to be investigated."""
    seed_event(database)
    found = assessment(healthy(candidate()))

    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.record_catalyst_assessment(found.as_row()) is True
        assert repository.record_catalyst_assessment(found.as_row()) is False

    with database.transaction() as session:
        assert len(StorageRepository(session).list_catalyst_assessments()) == 1


def test_an_assessment_cannot_outlive_the_anomaly_it_explains(database: Database) -> None:
    """A catalyst for an event nobody recorded explains nothing."""
    found = assessment(healthy(candidate()))

    with pytest.raises(IntegrityError):
        with database.transaction() as session:
            StorageRepository(session).record_catalyst_assessment(found.as_row())


# --- the database refuses the confusion too ------------------------------------------------------


def test_an_outage_cannot_be_stored_as_a_negative_finding(database: Database) -> None:
    """The mistake this design exists to prevent, refused at the storage layer.

    The model refuses to construct it, and that is tested elsewhere. This matters because a
    migration, a repair script or a future writer can all reach this table without the model,
    and 'no public catalyst' beside a rate-limited provider is a manufactured discovery whoever
    wrote it -- one that would cluster exactly when the provider was struggling.
    """
    seed_event(database)

    with pytest.raises(IntegrityError):
        store(database, row(unhealthy_sources="alpaca-news=RATE_LIMITED"))


def test_an_unplaceable_item_cannot_be_stored_as_silence(database: Database) -> None:
    """An item that cannot be ordered against the event cannot support a claim about it."""
    seed_event(database)

    with pytest.raises(IntegrityError):
        store(database, row(unplaceable=2))


def test_a_positive_finding_must_name_what_it_found(database: Database) -> None:
    """And a non-positive one must not name anything, which is the same constraint."""
    seed_event(database)

    with pytest.raises(IntegrityError):
        store(database, row(outcome="CATALYST_FOUND", catalyst_ids=""))
    with pytest.raises(IntegrityError):
        store(database, row(outcome="CATALYST_UNKNOWN", catalyst_ids="12345"))


def test_a_window_with_no_width_is_refused(database: Database) -> None:
    """A zero-length lookback would make 'nothing was published before it' trivially true."""
    seed_event(database)

    with pytest.raises(IntegrityError):
        store(database, row(lookback_seconds=0))


def test_two_answers_for_the_same_event_and_window_cannot_both_stand(
    database: Database,
) -> None:
    """One question has one answer. A second row for the same window would let a later reader
    pick whichever outcome suited them.
    """
    seed_event(database)
    store(database, row())

    with pytest.raises(IntegrityError):
        store(database, row(assessment_id="catalyst-v1-second", detail="a different answer"))


def test_a_different_window_is_a_different_question(database: Database) -> None:
    """Widening the lookback asks something else, and gets its own row rather than a conflict."""
    seed_event(database)
    store(database, row())
    store(database, row(assessment_id="catalyst-v1-wider", lookback_seconds=86400))

    with database.transaction() as session:
        assert len(StorageRepository(session).list_catalyst_assessments()) == 2


def test_assessments_can_be_filtered_by_outcome(database: Database) -> None:
    """`NO_PUBLIC_CATALYST_FOUND` is the interesting population, so it has to be findable."""
    seed_event(database)
    store(database, row())
    store(
        database,
        row(
            assessment_id="catalyst-v1-unknown",
            lookback_seconds=7200,
            outcome="CATALYST_UNKNOWN",
            unhealthy_sources="alpaca-news=TIMEOUT",
        ),
    )

    with database.transaction() as session:
        repository = StorageRepository(session)
        negatives = repository.list_catalyst_assessments(outcome="NO_PUBLIC_CATALYST_FOUND")
        unknowns = repository.list_catalyst_assessments(outcome="CATALYST_UNKNOWN")

    assert len(negatives) == 1 and len(unknowns) == 1
    assert negatives[0]["unhealthy_sources"] == ""


def test_an_unknown_outcome_may_sit_beside_an_unhealthy_source(database: Database) -> None:
    """The constraint must bind only the finding, not every row.

    `CATALYST_UNKNOWN` beside a failed source is the correct, expected pairing -- and a
    constraint that refused it would make honest recording impossible, which is the failure mode
    of over-tight validation.
    """
    seed_event(database)
    store(
        database,
        row(outcome="CATALYST_UNKNOWN", unhealthy_sources="alpaca-news=RATE_LIMITED"),
    )

    with database.transaction() as session:
        assert len(StorageRepository(session).list_catalyst_assessments()) == 1


def test_the_migration_is_additive(tmp_path: Path) -> None:
    """A real 0019 store through the real migration."""
    from alembic import command
    from alembic.config import Config

    from quantbot.storage.schema import SCHEMA_VERSION

    path = tmp_path / "v19.db"
    config = Config()
    config.set_main_option(
        "script_location", str(Path("src/quantbot/storage/migrations").resolve())
    )
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path.as_posix()}")
    command.upgrade(config, "0019")

    database = Database.migrate(path)
    try:
        with database.transaction() as session:
            assert StorageRepository(session).list_catalyst_assessments() == []
        with database.engine.connect() as connection:
            marker = connection.exec_driver_sql("SELECT version FROM schema_version").fetchone()
    finally:
        database.close()

    assert marker is not None and marker[0] == SCHEMA_VERSION


def test_the_assessment_is_content_addressed_over_its_answer() -> None:
    """A different answer to the same question is a different assessment.

    Two lookups minutes apart can legitimately differ -- the provider had nothing, then had
    something -- and collapsing them would destroy the record that the second found what the
    first could not.
    """
    nothing = assessment(healthy())
    something = assessment(healthy(candidate()))

    assert nothing.identity != something.identity
    assert nothing.outcome is CatalystOutcome.NO_PUBLIC_CATALYST_FOUND
    assert something.outcome is CatalystOutcome.CATALYST_FOUND
