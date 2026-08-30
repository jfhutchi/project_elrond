"""The discovery surface, and the two ways it could flatter the subsystems it renders (#56, #82).

Both subsystems have failure modes that look like success from a total. A run of
`NO_PUBLIC_CATALYST_FOUND` reads as a stream of discoveries; a run of `CATALYST_UNKNOWN` reads as
nothing happening. So the page shows the outcome mix rather than a count, and the tests below are
mostly about the distinctions that mix is there to preserve.

The panel also exists for a specific operational reason: the coverage gate and the sector-roundup
limitation recorded in `REFUTED.md` are both things an operator would only notice by looking at
what enrichment actually produced.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quantbot.anomaly.catalyst import (
    CatalystCandidate,
    SourceHealth,
    SourceResult,
    assess_catalysts,
)
from quantbot.console.app import create_app
from quantbot.console.state import read_console_state
from quantbot.storage import Database, StorageRepository
from tests.unit.anomaly.test_persistence import NOW as EVENT_OBSERVED
from tests.unit.anomaly.test_persistence import event as anomaly_event


@pytest.fixture
def database(tmp_path: Path):
    db = Database.create(tmp_path / "discovery.db")
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def client(database: Database) -> TestClient:
    return TestClient(create_app(database))


def state(database: Database):
    with database.transaction() as session:
        return read_console_state(session, now=datetime(2026, 8, 30, tzinfo=UTC))


def record_event(database: Database, symbol: str = "SPY"):
    event = anomaly_event(symbol=symbol)
    with database.transaction() as session:
        StorageRepository(session).record_anomaly_event(event.as_row())
    return event


def record_assessment(database: Database, event, source: SourceResult) -> None:
    assessment = assess_catalysts(
        anomaly_event_id=event.event_id, observed_at=EVENT_OBSERVED, sources=[source]
    )
    with database.transaction() as session:
        StorageRepository(session).record_catalyst_assessment(assessment.as_row())


def catalyst() -> CatalystCandidate:
    return CatalystCandidate(
        source="benzinga",
        external_id="1",
        published_at=EVENT_OBSERVED.replace(hour=14),
        retrieved_at=EVENT_OBSERVED,
        headline="Something was announced",
    )


# --- the distinctions the panel exists to preserve ---------------------------------------------


def test_an_anomaly_nobody_assessed_is_not_an_anomaly_with_no_catalyst(
    database: Database,
) -> None:
    """The distinction the whole panel turns on.

    "Nobody looked" and "somebody looked and found nothing" are different facts, and a page
    showing only the assessments would make the first invisible -- so an unenriched system would
    look like one that had investigated everything and found nothing to report.
    """
    record_event(database)

    panel = state(database).discovery

    assert panel.anomalies == 1
    assert panel.unassessed == 1
    assert panel.catalyst_outcomes == {}
    assert panel.findings == 0


def test_a_negative_finding_is_counted_only_when_one_was_actually_made(
    database: Database,
) -> None:
    """`findings` is the number that would be quoted, so it must not include unknowns."""
    event = record_event(database)
    record_assessment(
        database, event, SourceResult(source="alpaca-news", health=SourceHealth.HEALTHY)
    )

    panel = state(database).discovery

    assert panel.findings == 1
    assert panel.unassessed == 0
    assert panel.catalyst_outcomes == {"NO_PUBLIC_CATALYST_FOUND": 1}


def test_an_unknown_outcome_is_never_counted_as_a_finding(database: Database) -> None:
    """An outage that read as a discovery is the failure the three states exist to prevent."""
    event = record_event(database)
    record_assessment(
        database,
        event,
        SourceResult(
            source="alpaca-news",
            health=SourceHealth.RATE_LIMITED,
            detail="the provider rate-limited this lookup",
        ),
    )

    panel = state(database).discovery

    assert panel.findings == 0
    assert panel.catalyst_outcomes == {"CATALYST_UNKNOWN": 1}


def test_an_uncovered_symbol_is_named_rather_than_counted(database: Database) -> None:
    """Which instruments are unenrichable is the actionable part; a count is not.

    An operator seeing "3 uncovered" can do nothing with it. Seeing MDY, EFA, EEM tells them the
    negative findings they will never get, and for which instruments.
    """
    event = record_event(database, symbol="MDY")
    record_assessment(
        database,
        event,
        SourceResult(
            source="alpaca-news",
            health=SourceHealth.STALE,
            detail="no item about MDY in the trailing 90 days",
        ),
    )

    panel = state(database).discovery

    assert panel.uncovered_symbols == ("MDY",)
    assert panel.findings == 0


def test_a_covered_symbol_is_not_reported_as_uncovered(database: Database) -> None:
    """The gate is about a source's silence, not about every unhealthy answer.

    A rate limit is an outage, not a coverage gap, and conflating them would tell an operator to
    go find another data vendor for a symbol that is perfectly well covered.
    """
    event = record_event(database, symbol="SPY")
    record_assessment(
        database,
        event,
        SourceResult(
            source="alpaca-news", health=SourceHealth.TIMEOUT, detail="could not be reached"
        ),
    )

    assert state(database).discovery.uncovered_symbols == ()


# --- what reaches the operator -----------------------------------------------------------------


def test_unassessed_anomalies_reach_the_attention_list(database: Database) -> None:
    """A silently unenriched subsystem is the case an operator would never go looking for."""
    record_event(database)

    assert any("no catalyst assessment" in item for item in state(database).attention)


def test_uncovered_symbols_reach_the_attention_list(database: Database) -> None:
    """It bounds what the subsystem can ever conclude, which is worth surfacing once."""
    event = record_event(database, symbol="MDY")
    record_assessment(
        database,
        event,
        SourceResult(
            source="alpaca-news", health=SourceHealth.STALE, detail="no item in 90 days"
        ),
    )

    assert any("does not cover MDY" in item for item in state(database).attention)


def test_a_contained_failure_is_visible_rather_than_silent(database: Database) -> None:
    """Containment only protects the dataset if somebody can see the hole it left."""
    with database.transaction() as session:
        StorageRepository(session).create_incident(
            incident_id="anomaly-detection:1",
            severity="WARNING",
            kind="anomaly-detection",
            message="anomaly detection failed for this cycle",
            occurred_at=datetime(2026, 8, 29, 12, tzinfo=UTC),
        )

    console = state(database)

    assert [item.kind for item in console.incidents] == ["anomaly-detection"]
    assert any("unresolved incident" in item for item in console.attention)


# --- the page ------------------------------------------------------------------------------------


def test_the_page_renders_and_absence_reads_as_absence(client: TestClient) -> None:
    """An empty discovery panel must not read as a system that investigated and found nothing."""
    response = client.get("/discovery")

    assert response.status_code == 200
    assert "No catalyst assessment has been recorded" in response.text
    assert "not a claim that every symbol is covered" in response.text


def test_the_page_says_positive_findings_on_sector_etfs_are_not_evidence(
    client: TestClient, database: Database
) -> None:
    """The unfixed limitation is carried on the surface that reports the number it qualifies.

    A daily "Leading And Lagging Sectors" roundup satisfies the qualifying-window test and would
    render as `CATALYST_FOUND`. Recording that only in REFUTED.md would leave the console
    reporting a number more confident than the thing behind it.
    """
    event = record_event(database, symbol="XLU")
    record_assessment(
        database,
        event,
        SourceResult(
            source="alpaca-news", health=SourceHealth.HEALTHY, candidates=(catalyst(),)
        ),
    )

    response = client.get("/discovery")

    assert "CATALYST_FOUND" in response.text
    assert "not evidence a catalyst existed" in response.text


def test_the_page_is_reachable_from_the_navigation(client: TestClient) -> None:
    """A surface nobody can reach reports to nobody."""
    assert '/discovery' in client.get("/").text
