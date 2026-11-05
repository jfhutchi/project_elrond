"""Cleanup is restricted to complete membership histories of proven pre-POST refusals."""

import json
import os
import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest
from scripts.offline_guardian_projection_cleanup import (
    cleanup_copy,
    delete_selected,
    preflight,
    rebuild_selected,
    verify_cleanup,
)
from scripts.offline_sqlite_compaction import verify_equivalent

from quantbot.domain import IntentState
from quantbot.paper_core.guardian import acknowledge_handoffs
from quantbot.paper_core.send_guard import record_pre_send_refusal
from quantbot.risk import OrderPurpose
from quantbot.storage import StorageRepository
from tests.unit.paper_core.test_guardian_retention import NOW, claimed

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="cleanup requires the Windows deny-write/delete file lease"
)


@pytest.fixture
def closed_refusal(tmp_path):
    db, store, entry, plan = claimed(tmp_path)
    first = acknowledge_handoffs(store, now=NOW + timedelta(seconds=1))
    second = acknowledge_handoffs(store, now=NOW + timedelta(seconds=62))
    with db.transaction() as session:
        repo = StorageRepository(session)
        intent = repo.transition_order_intent(
            entry.intent.intent_id,
            IntentState.ORDER_CREATED,
            expected_current=IntentState.RISK_APPROVED,
        )
        record_pre_send_refusal(
            session, intent, OrderPurpose.ENTRY, ("EXPIRED",), clock=lambda: NOW
        )
        repo.transition_order_intent(
            intent.intent_id, IntentState.ERROR, expected_current=IntentState.ORDER_CREATED
        )
    acknowledge_handoffs(store, now=NOW + timedelta(seconds=63))
    path = store.database.path
    db.close()
    from pathlib import Path

    path = Path(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA journal_mode=DELETE")
    return path, entry, plan, first, second


def test_cleanup_only_memberships_roundtrip_and_reinsertion_hazard(closed_refusal, tmp_path):
    source, entry, plan, first, second = closed_refusal
    before = source.read_bytes()
    report = preflight(source)
    assert report["eligible_plan_ids"] == [plan.id]
    candidate = tmp_path / "candidate.db"
    result = cleanup_copy(source, candidate)
    assert result["removed_memberships"] == 2
    assert source.read_bytes() == before
    with closing(sqlite3.connect(candidate)) as connection, connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM core_guardian_memberships WHERE plan_id=?", (plan.id,)
            ).fetchone()[0]
            == 0
        )
    restored = tmp_path / "rebuilt.db"
    rebuild_selected(candidate, restored, [plan.id])
    verify_equivalent(source, restored)
    with closing(sqlite3.connect(candidate)) as connection, connection:
        connection.execute("INSERT INTO core_guardian_memberships VALUES(?,?)", (plan.id, first.id))
    with pytest.raises(ValueError, match="selected|membership"):
        verify_cleanup(source, candidate, [plan.id])


@pytest.mark.parametrize(
    "unsafe",
    [
        "no-refusal",
        "sparse-refusal",
        "wrong-source",
        "broker-fill",
        "contradictory-event",
        "outcome",
        "wrapper",
        "plan-hash",
        "quarantine",
    ],
)
def test_unsafe_plan_is_never_selected(closed_refusal, unsafe):
    path, entry, plan, first, second = closed_refusal
    with closing(sqlite3.connect(path)) as connection, connection:
        if unsafe == "no-refusal":
            connection.execute("DELETE FROM order_events")
        elif unsafe in {"sparse-refusal", "wrong-source"}:
            detail = json.loads(
                connection.execute(
                    "SELECT detail_json FROM order_events "
                    "WHERE event_type='CORE_ENTRY_SEND_REFUSED'"
                ).fetchone()[0]
            )
            if unsafe == "sparse-refusal":
                detail.pop("state_at_refusal")
            else:
                detail["source"] = "not-the-pre-post-boundary"
            connection.execute("UPDATE order_events SET detail_json=?", (json.dumps(detail),))
        elif unsafe == "broker-fill":
            # A contradictory real order/fill must remain byte-for-byte retained.
            connection.execute(
                "INSERT INTO broker_orders VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "real-order",
                    entry.intent.client_order_id,
                    entry.intent.symbol,
                    "BUY",
                    "MARKET",
                    "DAY",
                    "1",
                    "1",
                    "filled",
                    NOW.isoformat(),
                    "100",
                ),
            )
            connection.execute(
                "INSERT INTO fills(fill_id,broker_order_id,symbol,side,quantity,price,"
                "occurred_at,fee_source) "
                "VALUES('real-fill','real-order',?,'BUY','1','100',?,'UNKNOWN')",
                (entry.intent.symbol, NOW.isoformat()),
            )
        elif unsafe == "contradictory-event":
            connection.execute(
                "INSERT INTO order_events "
                "VALUES('contradiction',?,NULL,'BROKER_POST_ATTEMPTED',?, '{}')",
                (entry.intent.intent_id, NOW.isoformat()),
            )
        elif unsafe == "outcome":
            connection.execute(
                "INSERT INTO core_round_trips VALUES(?,?)",
                (plan.decision_id, json.dumps({"lot_id": plan.decision_id})),
            )
        elif unsafe == "wrapper":
            connection.execute(
                "UPDATE core_intent_links "
                "SET payload=replace(payload,'paper-core-synthetic','wrong-owner')"
            )
        elif unsafe == "plan-hash":
            connection.execute(
                "UPDATE core_protection_plans "
                "SET payload=replace(payload,'paper-core-synthetic','wrong-owner')"
            )
        else:
            connection.execute(
                "INSERT INTO incidents(incident_id,severity,kind,message,occurred_at,detail_json) "
                "VALUES('quarantine','ERROR','CORE_EVIDENCE_CONFLICT','fixture',?,?)",
                (NOW.isoformat(), json.dumps({"lot_ids": [plan.decision_id]})),
            )
    assert preflight(path)["eligible_plan_ids"] == []


def test_sparse_projection_and_unresolved_submission_refuse_before_copy(closed_refusal, tmp_path):
    path, entry, plan, first, second = closed_refusal
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DELETE FROM core_guardian_memberships WHERE receipt_id=?", (first.id,))
    with pytest.raises(ValueError, match="projection|membership"):
        cleanup_copy(path, tmp_path / "refused.db")
    assert not (tmp_path / "refused.db").exists()


def test_delete_batch_is_atomic_on_interruption(closed_refusal):
    path, entry, plan, first, second = closed_refusal
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER stop_cleanup BEFORE DELETE ON core_guardian_memberships "
            "WHEN old.receipt_id='" + second.id + "' BEGIN SELECT RAISE(ABORT,'interrupted'); END"
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="interrupted"):
            delete_selected(connection, [plan.id])
        assert (
            connection.execute(
                "SELECT count(*) FROM core_guardian_memberships WHERE plan_id=?", (plan.id,)
            ).fetchone()[0]
            == 2
        )


def test_zero_unresolved_intents_is_required(closed_refusal, tmp_path):
    path, entry, plan, *_ = closed_refusal
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE order_intents SET state='SUBMISSION_UNKNOWN'")
    with pytest.raises(ValueError, match="zero unresolved"):
        cleanup_copy(path, tmp_path / "refused.db")
    assert not (tmp_path / "refused.db").exists()


def test_unrelated_real_fill_is_unchanged_while_refused_projection_removed(
    closed_refusal, tmp_path
):
    path, entry, plan, *_ = closed_refusal
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        row = dict(connection.execute("SELECT * FROM order_intents").fetchone())
        row.update(
            intent_id="real-trade",
            client_order_id="real-client",
            strategy_id="other-owner",
            state="FILLED",
        )
        connection.execute(
            "INSERT INTO order_intents("
            + ",".join(row)
            + ") VALUES("
            + ",".join("?" for _ in row)
            + ")",
            tuple(row.values()),
        )
        connection.execute(
            "INSERT INTO broker_orders "
            "VALUES('real-order','real-client',?,'BUY','MARKET','DAY','1','1','filled',?,'100')",
            (entry.intent.symbol, NOW.isoformat()),
        )
        connection.execute(
            "INSERT INTO fills(fill_id,broker_order_id,symbol,side,quantity,price,"
            "occurred_at,fee_source) "
            "VALUES('real-fill','real-order',?,'BUY','1','100',?,'UNKNOWN')",
            (entry.intent.symbol, NOW.isoformat()),
        )
    report = cleanup_copy(path, tmp_path / "candidate.db")
    assert report["removed_memberships"] == 2
    assert report["tables"]["fills"]["unchanged_rows"] == 1
    assert report["tables"]["broker_orders"]["unchanged_rows"] == 1


def test_raw_fallback_and_guardian_renewal_do_not_reinsert_retired_memberships(
    closed_refusal, tmp_path
):
    from types import SimpleNamespace

    from sqlalchemy import select

    from quantbot.paper_core.contracts import CoreConfig
    from quantbot.paper_core.store import CoreStore, table
    from quantbot.paper_core.work import historical_receipts
    from quantbot.storage import Database

    path, entry, plan, first, second = closed_refusal
    candidate = tmp_path / "candidate.db"
    cleanup_copy(path, candidate)
    db = Database(candidate)
    try:
        with db.transaction() as session:
            config = CoreConfig.model_validate_json(
                session.execute(select(table("core_identities").c.payload)).scalar_one()
            )
            assert {receipt.id for receipt in historical_receipts(session, plan.id)} == {
                first.id,
                second.id,
            }
        store = CoreStore(db, config)
        store.runtime_role = "guardian"
        store.clocks = SimpleNamespace(sample=lambda: (NOW + timedelta(seconds=125), 125.0))
        renewed = acknowledge_handoffs(store, now=NOW + timedelta(seconds=125))
        assert renewed.protection_ids == ()
        acknowledge_handoffs(store, now=NOW + timedelta(seconds=190))
        with db.transaction() as session:
            assert session.execute(select(table("core_guardian_memberships"))).all() == []
            assert {receipt.id for receipt in historical_receipts(session, plan.id)} == {
                first.id,
                second.id,
            }
    finally:
        db.close()


def test_source_read_lease_denies_writes_and_delete(closed_refusal):
    from scripts.offline_guardian_projection_cleanup import read_lease

    path, *_ = closed_refusal
    with read_lease(path):
        with pytest.raises(PermissionError):
            path.open("r+b")
        with pytest.raises(PermissionError):
            path.unlink()


def test_dry_run_budget_fails_incomplete_without_source_change(closed_refusal):
    path, *_ = closed_refusal
    before = path.read_bytes()
    with pytest.raises(TimeoutError, match="incomplete"):
        preflight(path, seconds=0)
    assert path.read_bytes() == before


def test_cleanup_runs_one_source_audit_and_streams_existing_pk_order(
    closed_refusal, tmp_path, monkeypatch
):
    import scripts.offline_guardian_projection_cleanup as cleanup

    path, *_ = closed_refusal
    statements, phases = [], []
    original = cleanup.connect

    def traced(source):
        connection = original(source)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(cleanup, "connect", traced)
    cleanup_copy(path, tmp_path / "candidate.db", progress=phases.append)
    assert sum(item["phase"] == "source_integrity" for item in phases) == 1
    assert sum(item["phase"] == "source_receipts" for item in phases) == 1
    assert not any("ORDER BY receipt_id,plan_id" in query for query in statements)
    with closing(sqlite3.connect(path)) as connection, connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT plan_id,receipt_id FROM core_guardian_memberships "
            "ORDER BY plan_id,receipt_id"
        ).fetchall()
    assert not any("TEMP B-TREE" in str(row) for row in plan)


@pytest.mark.parametrize("bad_binding", [None, "decision", "plan", "receipt"])
def test_exact_canonical_final_send_refusal_requires_all_bindings(closed_refusal, bad_binding):
    path, entry, plan, first, second = closed_refusal
    with closing(sqlite3.connect(path)) as connection, connection:
        detail = json.loads(
            connection.execute("SELECT detail_json FROM order_events").fetchone()[0]
        )
        detail.pop("source")
        detail.pop("purpose")
        detail.update(
            decision_id=plan.decision_id, protection_id=plan.id, guardian_receipt_id=first.id
        )
        if bad_binding:
            detail[
                {
                    "decision": "decision_id",
                    "plan": "protection_id",
                    "receipt": "guardian_receipt_id",
                }[bad_binding]
            ] = "0" * 64
        connection.execute("UPDATE order_events SET detail_json=?", (json.dumps(detail),))
    assert preflight(path)["eligible_plan_ids"] == ([plan.id] if bad_binding is None else [])


def test_sql_progress_deadline_returns_incomplete(closed_refusal, monkeypatch):
    import scripts.offline_guardian_projection_cleanup as cleanup

    path, *_ = closed_refusal

    def costly_check(connection):
        connection.execute(
            "WITH RECURSIVE n(i) AS (VALUES(0) UNION ALL "
            "SELECT i+1 FROM n WHERE i<100000000) SELECT sum(i) FROM n"
        ).fetchone()

    monkeypatch.setattr(cleanup, "check", costly_check)
    with pytest.raises(TimeoutError, match="incomplete"):
        preflight(path, seconds=0.02)


def test_same_count_projection_substitution_is_detected(closed_refusal):
    path, entry, plan, first, second = closed_refusal
    with closing(sqlite3.connect(path)) as connection, connection:
        empty = connection.execute(
            "SELECT id FROM core_guardian_receipts WHERE "
            "json_array_length(json_extract(payload,'$.protection_ids'))=0 LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            "UPDATE core_guardian_memberships SET receipt_id=? WHERE plan_id=? AND receipt_id=?",
            (empty, plan.id, first.id),
        )
    with pytest.raises(ValueError, match="membership projection"):
        preflight(path)


def test_zero_selected_copy_and_candidate_raw_corruption_are_checked(closed_refusal, tmp_path):
    path, *_ = closed_refusal
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DELETE FROM order_events")
    candidate = tmp_path / "candidate.db"
    report = cleanup_copy(path, candidate)
    assert report["selected_plan_ids"] == []
    verify_equivalent(path, candidate)
    with closing(sqlite3.connect(candidate)) as connection, connection:
        connection.execute(
            "UPDATE core_guardian_receipts "
            "SET payload=replace(payload,'paper-core-synthetic','changed-owner')"
        )
    with pytest.raises(ValueError, match="core_guardian_receipts"):
        verify_cleanup(path, candidate, [])


def test_file_backed_dedicated_scratch_and_environment_restored(
    closed_refusal, tmp_path, monkeypatch
):
    import os

    import scripts.offline_guardian_projection_cleanup as cleanup

    path, *_ = closed_refusal
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    before = {key: os.environ.get(key) for key in ("TMP", "TEMP", "SQLITE_TMPDIR")}
    actual_check = cleanup.check
    observations = []

    def inspect_configuration(connection):
        assert connection.execute("PRAGMA temp_store").fetchone()[0] == 1
        directory = connection.execute("PRAGMA temp_store_directory").fetchone()[0]
        from pathlib import Path

        assert Path(directory).resolve().is_relative_to(scratch.resolve())
        assert all(os.environ[key] == directory for key in before)
        observations.append(directory)
        actual_check(connection)

    monkeypatch.setattr(cleanup, "check", inspect_configuration)
    preflight(path, scratch_directory=scratch)
    assert observations
    assert {key: os.environ.get(key) for key in before} == before
    assert list(scratch.iterdir()) == []


def test_unavailable_scratch_refuses_before_source_integrity(closed_refusal, tmp_path, monkeypatch):
    import scripts.offline_guardian_projection_cleanup as cleanup

    path, *_ = closed_refusal
    monkeypatch.setattr(
        cleanup, "check", lambda connection: pytest.fail("integrity started without scratch")
    )
    with pytest.raises(ValueError, match="scratch"):
        preflight(path, scratch_directory=tmp_path / "does-not-exist")


def test_bounded_rowid_windows_cover_empty_windows_and_rollback_all():
    with closing(sqlite3.connect(":memory:")) as connection, connection:
        connection.execute(
            "CREATE TABLE core_guardian_memberships(plan_id TEXT,receipt_id TEXT,"
            "PRIMARY KEY(plan_id,receipt_id))"
        )
        rows = [("selected" if i < 3 or i > 18 else "retained", str(i)) for i in range(24)]
        connection.executemany("INSERT INTO core_guardian_memberships VALUES(?,?)", rows)
        connection.commit()
        connection.execute(
            "CREATE TRIGGER stop_cleanup BEFORE DELETE ON core_guardian_memberships "
            "WHEN old.receipt_id='21' BEGIN SELECT RAISE(ABORT,'interrupted'); END"
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="interrupted"):
            delete_selected(connection, {"selected"}, window_size=3)
        assert (
            connection.execute("SELECT count(*) FROM core_guardian_memberships").fetchone()[0] == 24
        )
        connection.execute("DROP TRIGGER stop_cleanup")
        connection.commit()
        delete_selected(connection, {"selected"}, window_size=3)
        assert (
            connection.execute("SELECT count(*) FROM core_guardian_memberships").fetchone()[0] == 16
        )
        scan = connection.execute(
            "EXPLAIN SELECT rowid,plan_id FROM core_guardian_memberships "
            "WHERE rowid>? ORDER BY rowid LIMIT ?",
            (0, 3),
        ).fetchall()
        delete = connection.execute(
            "EXPLAIN DELETE FROM core_guardian_memberships WHERE rowid IN(?,?,?)", (1, 2, 3)
        ).fetchall()
        assert "SeekGT" in {row[1] for row in scan}
        assert "DecrJumpZero" in {row[1] for row in scan}
        assert "SeekRowid" in {row[1] for row in delete}
        assert sum(row[1] == "IdxInsert" for row in delete) <= 3


def test_memory_only_sqlite_compile_refuses_and_restores_environment(closed_refusal, monkeypatch):
    import os

    import scripts.offline_guardian_projection_cleanup as cleanup

    path, *_ = closed_refusal
    before = {key: os.environ.get(key) for key in ("TMP", "TEMP", "SQLITE_TMPDIR")}

    class MemoryOnlyBuild:
        def execute(self, statement):
            assert statement == "PRAGMA compile_options"
            return [("TEMP_STORE=3",)]

        def close(self):
            pass

    monkeypatch.setattr(cleanup.sqlite3, "connect", lambda path: MemoryOnlyBuild())
    with pytest.raises(ValueError, match="memory-only"):
        with cleanup.file_scratch(path, cleanup.Budget()):
            pytest.fail("memory-only SQLite was accepted")
    assert {key: os.environ.get(key) for key in before} == before


def test_sql_progress_emits_heartbeat_during_long_phase(monkeypatch):
    import scripts.offline_guardian_projection_cleanup as cleanup

    clock, progress = [0.0], []
    monkeypatch.setattr(cleanup.time, "monotonic", lambda: clock[0])
    budget = cleanup.Budget(progress=progress.append)
    budget.tick(phase="source_integrity")
    with closing(sqlite3.connect(":memory:")) as connection:
        budget.attach(connection)
        clock[0] = 6.0
        connection.execute(
            "WITH RECURSIVE n(i) AS (VALUES(0) UNION ALL "
            "SELECT i+1 FROM n WHERE i<10000) SELECT sum(i) FROM n"
        ).fetchone()
    assert len(progress) >= 2
    assert progress[-1]["phase"] == "source_integrity"


def test_deletion_progress_counts_scanned_rows_and_preserves_atomic_rollback():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(
            "CREATE TABLE core_guardian_memberships(plan_id TEXT,receipt_id TEXT,"
            "PRIMARY KEY(plan_id,receipt_id))"
        )
        rows = [("remove" if i % 2 else "keep", str(i)) for i in range(7)]
        connection.executemany("INSERT INTO core_guardian_memberships VALUES(?,?)", rows)
        connection.commit()
        samples = []

        def interrupted(scanned, deleted):
            samples.append((scanned, deleted))
            raise TimeoutError("budget expired after a bounded window")

        with pytest.raises(TimeoutError, match="bounded window"):
            delete_selected(connection, {"remove"}, window_size=3, progress=interrupted)
        assert samples == [(3, 1)]
        assert connection.execute("SELECT * FROM core_guardian_memberships").fetchall() == rows
        samples.clear()
        delete_selected(
            connection,
            {"remove"},
            window_size=3,
            progress=lambda scanned, deleted: samples.append((scanned, deleted)),
        )
        assert samples == [(3, 1), (6, 3), (7, 3)]


def test_sql_heartbeat_retains_latest_count_until_next_phase(monkeypatch):
    import scripts.offline_guardian_projection_cleanup as cleanup

    clock, samples = [0.0], []
    monkeypatch.setattr(cleanup.time, "monotonic", lambda: clock[0])
    budget = cleanup.Budget(progress=samples.append)
    budget.tick(phase="delete_memberships")
    budget.tick(4096, deleted=3500)
    clock[0] = 6.0
    budget.tick()
    assert samples[-1]["rows"] == 4096 and samples[-1]["deleted_rows"] == 3500
    budget.tick(phase="vacuum_candidate")
    assert samples[-1]["rows"] == 0 and samples[-1]["deleted_rows"] == 0


def verified_receipt(path, output):
    import hashlib

    output.write_text(
        json.dumps(
            {
                "kind": "ELROND_VERIFIED_SQLITE_BACKUP_V1",
                "backup": str(path.resolve()),
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "integrity_check": "ok",
                "foreign_key_check": "ok",
                "sqlite_version": sqlite3.sqlite_version,
                "checks_complete": True,
            }
        )
    )
    return output


def test_verified_source_receipt_rechecks_hash_and_only_skips_source_integrity(
    closed_refusal, tmp_path, monkeypatch
):
    import hashlib

    import scripts.offline_guardian_projection_cleanup as cleanup

    path, *_ = closed_refusal
    receipt = verified_receipt(path, tmp_path / "verified.json")
    actual_check, checked = cleanup.check, []

    def candidate_only(connection):
        database = connection.execute("PRAGMA database_list").fetchone()[2]
        assert database != str(path)
        checked.append(database)
        actual_check(connection)

    monkeypatch.setattr(cleanup, "check", candidate_only)
    report = cleanup_copy(path, tmp_path / "candidate.db", verified_backup_receipt=receipt)
    assert len(checked) == 1
    assert report["source_integrity_reused"] is True
    assert report["source_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert (
        report["verified_backup_receipt_sha256"] == hashlib.sha256(receipt.read_bytes()).hexdigest()
    )


@pytest.mark.parametrize("changed", ["hash", "path", "size", "incomplete", "status", "schema"])
def test_verified_receipt_mismatch_fails_without_integrity_fallback(
    closed_refusal, tmp_path, monkeypatch, changed
):
    import scripts.offline_guardian_projection_cleanup as cleanup

    path, *_ = closed_refusal
    receipt = verified_receipt(path, tmp_path / "verified.json")
    payload = json.loads(receipt.read_text())
    if changed == "hash":
        payload["sha256"] = "0" * 64
    elif changed == "path":
        payload["backup"] = str(tmp_path / "other.db")
    elif changed == "size":
        payload["bytes"] += 1
    elif changed == "incomplete":
        payload["checks_complete"] = False
    elif changed == "status":
        payload["foreign_key_check"] = "not-checked"
    else:
        payload["extra"] = "not-allowed"
    receipt.write_text(json.dumps(payload))
    monkeypatch.setattr(
        cleanup, "check", lambda connection: pytest.fail("receipt mismatch silently fell back")
    )
    with pytest.raises(ValueError, match="receipt"):
        preflight(path, verified_backup_receipt=receipt)


def test_capacity_refuses_before_expensive_audit(closed_refusal, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import scripts.offline_guardian_projection_cleanup as cleanup

    path, *_ = closed_refusal
    monkeypatch.setattr(
        cleanup.shutil,
        "disk_usage",
        lambda directory: SimpleNamespace(free=2 * path.stat().st_size),
    )
    monkeypatch.setattr(
        cleanup, "_audit", lambda *args: pytest.fail("capacity refused after audit")
    )
    with pytest.raises(ValueError, match="capacity|space"):
        cleanup_copy(path, tmp_path / "candidate.db", vacuum=True)


def test_vacuum_peak_is_three_sizes_plus_headroom_and_split_by_volume(tmp_path, monkeypatch):
    import scripts.offline_guardian_projection_cleanup as cleanup

    size = 1024 * 1024 * 1024
    candidate, scratch = tmp_path / "candidate", tmp_path / "scratch"
    candidate.mkdir()
    scratch.mkdir()
    monkeypatch.setattr(cleanup, "volume_key", lambda path: "shared")
    plan = cleanup.capacity_requirements(size, candidate, scratch, vacuum=True)
    assert plan["volumes"]["shared"]["required_bytes"] == 3 * size + plan["headroom_bytes"]
    monkeypatch.setattr(
        cleanup, "volume_key", lambda path: "candidate" if path == candidate else "scratch"
    )
    plan = cleanup.capacity_requirements(size, candidate, scratch, vacuum=True)
    assert plan["volumes"]["candidate"]["required_bytes"] == 2 * size + plan["headroom_bytes"]
    assert plan["volumes"]["scratch"]["required_bytes"] == size + plan["headroom_bytes"]


def test_postcopy_space_drop_refuses_before_deletion(closed_refusal, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import scripts.offline_guardian_projection_cleanup as cleanup

    path, *_ = closed_refusal
    partial = tmp_path / "candidate.db.unverified"
    monkeypatch.setattr(
        cleanup.shutil,
        "disk_usage",
        lambda directory: SimpleNamespace(free=0 if partial.exists() else 10**15),
    )
    monkeypatch.setattr(
        cleanup, "delete_selected", lambda *args: pytest.fail("delete started after capacity drop")
    )
    with pytest.raises(ValueError, match="capacity|space"):
        cleanup_copy(path, tmp_path / "candidate.db")
    assert not (tmp_path / "candidate.db").exists()


def test_candidate_digest_is_bound_to_existing_verification_lease(
    closed_refusal, tmp_path, monkeypatch
):
    import hashlib

    import scripts.offline_guardian_projection_cleanup as cleanup

    path, *_ = closed_refusal
    real_digest, seen = cleanup.candidate_digest, []

    def checked_digest(candidate, budget):
        # The same Windows lease must still deny mutation after integrity/row verification.
        with pytest.raises(PermissionError):
            candidate.open("r+b")
        with pytest.raises(PermissionError):
            candidate.unlink()
        result = real_digest(candidate, budget)
        seen.append(result)
        return result

    monkeypatch.setattr(cleanup, "candidate_digest", checked_digest)
    candidate = tmp_path / "candidate.db"
    report = cleanup_copy(path, candidate)
    assert seen
    assert report["candidate_sha256"] == hashlib.sha256(candidate.read_bytes()).hexdigest()
    assert report["candidate_bytes"] == candidate.stat().st_size
    with candidate.open("ab") as output:
        output.write(b"changed after lease release")
    assert report["candidate_sha256"] != hashlib.sha256(candidate.read_bytes()).hexdigest()
    assert report["candidate_bytes"] != candidate.stat().st_size


def test_vacuum_rechecks_additional_space_after_delete_journal_is_gone(
    closed_refusal, tmp_path, monkeypatch
):
    import hashlib

    import scripts.offline_guardian_projection_cleanup as cleanup

    path, *_ = closed_refusal
    candidate = tmp_path / "vacuum.db"
    partial = tmp_path / "vacuum.db.unverified"
    actual_capacity, checked = cleanup.require_capacity, []

    def observe_capacity(plan):
        if plan["phase"] == "vacuum":
            assert not (tmp_path / "vacuum.db.unverified-journal").exists()
            assert len(plan["volumes"]) == 1
            assert (
                next(iter(plan["volumes"].values()))["required_bytes"]
                == 2 * partial.stat().st_size + plan["headroom_bytes"]
            )
            checked.append("vacuum")
        actual_capacity(plan)

    monkeypatch.setattr(cleanup, "require_capacity", observe_capacity)
    report = cleanup_copy(path, candidate, vacuum=True)
    assert checked == ["vacuum"]
    assert report["candidate_bytes"] == candidate.stat().st_size
    assert report["candidate_sha256"] == hashlib.sha256(candidate.read_bytes()).hexdigest()
