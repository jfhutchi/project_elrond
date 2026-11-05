"""Offline maintenance preserves history and rejects unsafe projections."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from scripts.offline_sqlite_compaction import compact_copy, inspect_copy, verify_equivalent
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from quantbot.paper_core.contracts import GuardianReceipt
from quantbot.paper_core.work import historical_receipts


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "offline.db"
    now = datetime(2026, 11, 5, tzinfo=UTC)
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE schema_version(id INTEGER PRIMARY KEY, version INTEGER,
                                        store_identity TEXT);
            INSERT INTO schema_version VALUES(1,37,'persistent-store');
            CREATE TABLE core_guardian_receipts(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE core_guardian_memberships(plan_id TEXT,receipt_id TEXT,
                PRIMARY KEY(plan_id,receipt_id),
                FOREIGN KEY(receipt_id) REFERENCES core_guardian_receipts(id));
            CREATE TABLE core_guardian_current(id TEXT PRIMARY KEY,receipt_id TEXT,
                FOREIGN KEY(receipt_id) REFERENCES core_guardian_receipts(id));
            CREATE TABLE fills(id TEXT PRIMARY KEY,price TEXT,quantity TEXT);
            INSERT INTO fills VALUES('historical-fill','101.23456789','2');
            CREATE TABLE performance(id TEXT PRIMARY KEY,payload BLOB);
            INSERT INTO performance VALUES('historical-trip',x'0001ff');
            CREATE TABLE churn(id INTEGER PRIMARY KEY,payload BLOB);
        """)
        for second in (0, 60):
            receipt = GuardianReceipt(
                owner="fixture",
                generation=1,
                recorded_at_utc=now + timedelta(seconds=second),
                valid_until=now + timedelta(seconds=second + 120),
                protection_ids=("historical-plan",),
            )
            connection.execute(
                "INSERT INTO core_guardian_receipts VALUES(?,?)",
                (receipt.id, receipt.model_dump_json()),
            )
            connection.execute(
                "INSERT INTO core_guardian_memberships VALUES(?,?)", ("historical-plan", receipt.id)
            )
        connection.execute("INSERT INTO core_guardian_current VALUES('current',?)", (receipt.id,))
        connection.executemany(
            "INSERT INTO churn VALUES(?,?)", ((i, b"x" * 4096) for i in range(200))
        )
        connection.execute("DELETE FROM churn")
    return path


def history(path):
    engine = create_engine("sqlite:///" + str(path))
    try:
        with Session(engine) as session:
            return {r.id for r in historical_receipts(session, "historical-plan")}
    finally:
        engine.dispose()


def test_compaction_restores_all_history_and_preserves_source(ledger, tmp_path):
    before = ledger.read_bytes()
    destination = tmp_path / "restored.db"
    report = compact_copy(ledger, destination)
    assert report["verified"] is True
    assert report["reclaimed_bytes"] > 0
    assert ledger.read_bytes() == before
    assert history(ledger) == history(destination)
    assert len(history(destination)) == 2
    assert report["tables"]["fills"]["rows"] == 1
    assert report["tables"]["performance"]["rows"] == 1


def test_sparse_projection_refuses_candidate(ledger, tmp_path):
    with sqlite3.connect(ledger) as connection:
        connection.execute(
            "DELETE FROM core_guardian_memberships WHERE receipt_id = "
            "(SELECT min(receipt_id) FROM core_guardian_memberships)"
        )
    destination = tmp_path / "refused.db"
    with pytest.raises(ValueError, match="membership"):
        compact_copy(ledger, destination)
    assert not destination.exists()


def test_same_row_count_payload_corruption_is_detected(ledger, tmp_path):
    destination = tmp_path / "restored.db"
    compact_copy(ledger, destination)
    with sqlite3.connect(destination) as connection:
        connection.execute("UPDATE fills SET price='999'")
    with pytest.raises(ValueError, match="fills"):
        verify_equivalent(ledger, destination)


def test_refuses_existing_output_and_wal_sidecar(ledger, tmp_path):
    with pytest.raises(ValueError, match="exists"):
        compact_copy(ledger, ledger)
    ledger.with_name(ledger.name + "-wal").write_bytes(b"uncheckpointed")
    with pytest.raises(ValueError, match="standalone"):
        compact_copy(ledger, tmp_path / "copy.db")


def test_receipt_identity_corruption_is_detected(ledger, tmp_path):
    with sqlite3.connect(ledger) as connection:
        connection.execute(
            "UPDATE core_guardian_receipts SET payload = replace(payload,'fixture','tampered')"
        )
    with pytest.raises(ValueError, match="receipt identity"):
        compact_copy(ledger, tmp_path / "copy.db")


def test_extra_projection_row_is_detected(ledger, tmp_path):
    with sqlite3.connect(ledger) as connection:
        connection.execute(
            "INSERT INTO core_guardian_memberships SELECT 'not-in-payload',"
            "min(id) FROM core_guardian_receipts"
        )
    with pytest.raises(ValueError, match="unexpected guardian membership"):
        compact_copy(ledger, tmp_path / "copy.db")


def test_inspector_records_free_pages_and_budget_without_writes(ledger):
    before = ledger.read_bytes()
    report = inspect_copy(ledger)
    assert report["free_page_bytes"] > 0
    assert report["additional_compaction_space_budget"] == 2 * len(before)
    assert report["dbstat_complete"] or report["dbstat_error"]
    assert ledger.read_bytes() == before
