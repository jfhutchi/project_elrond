"""Read real SQLite fixtures; catch duplicate, authority, cutoff and redaction mistakes."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime

import pytest

AS_OF = datetime(2026, 10, 14, tzinfo=UTC)
STAMP = "2026-10-09T15:45:32+00:00"


def api():
    assert importlib.util.find_spec("quantbot.paper_core") is not None, "M0 exporter missing"
    from quantbot.paper_core.inventory import CoreConfig, inspect_inventory, trace_legacy_lot
    return CoreConfig, inspect_inventory, trace_legacy_lot


def fixture_stores(tmp_path):
    ops = tmp_path / "operations.db"
    with sqlite3.connect(ops) as db:
        db.executescript("""
        CREATE TABLE schema_version(version INTEGER, store_identity TEXT);
        INSERT INTO schema_version VALUES(9999,'fixture-store');
        CREATE TABLE fills(fill_id TEXT, broker_order_id TEXT, symbol TEXT, side TEXT,
                           quantity TEXT, price TEXT, occurred_at TEXT, fee TEXT);
        CREATE TABLE order_intents(intent_id TEXT,client_order_id TEXT,symbol TEXT,
                                   side TEXT,created_at TEXT);
        CREATE TABLE runtime_deployments(role TEXT,git_commit TEXT,observed_at TEXT,
                                        process_started_at TEXT);
        CREATE TABLE kill_switch_state(engaged INTEGER,reason TEXT,updated_at TEXT);
        CREATE TABLE reconciliation_runs(reconciliation_id TEXT,status TEXT,completed_at TEXT);
        INSERT INTO fills VALUES('buy','order-buy','XLI','BUY','2','100',
                                 '2026-10-09T15:16:36+00:00','0');
        INSERT INTO fills VALUES('sell','order-sell','XLI','SELL','2','101',
                                 '2026-10-09T15:45:08+00:00','0');
        INSERT INTO order_intents VALUES('entry','client','XLI','BUY',
                                         '2026-10-09T15:16:35+00:00');
        INSERT INTO runtime_deployments VALUES('console','bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                            '2026-10-13T12:00:00+00:00','2026-10-13T11:00:00+00:00');
        INSERT INTO kill_switch_state VALUES(1,'secret-account-number',
                                             '2026-10-13T12:00:00+00:00');
        INSERT INTO reconciliation_runs VALUES('flat-proof','MATCHED',
                                                '2026-10-09T15:45:20+00:00');
        """)
    copy = tmp_path / "learner.db"
    shutil.copyfile(ops, copy)
    exp = tmp_path / "experiment.db"
    with sqlite3.connect(exp) as db:
        db.execute("CREATE TABLE events(experiment_id TEXT,idempotency_key TEXT,kind TEXT,"
                   "recorded_at TEXT,document TEXT,digest TEXT)")
        records = []
        for kind, order, side, fill, quantity in [
            ("ENTRY_DECLARED", "entry", None, None, "0"),
            ("FILL", "order-buy", "BUY", "buy", "2"),
            ("PROTECTION_BOUND", "entry", None, None, "2"),
            ("FILL", "order-sell", "SELL", "sell", "2"),
            ("RECONCILED_FLAT", "entry", None, None, "0"),
        ]:
            records.append(("LotEvent", dict(lot_id="lot", decision_id="observation",
                kind=kind, order_id=order, side=side, fill_id=fill, quantity=quantity,
                symbol="XLI", synthetic=False, occurred_at=STAMP, recorded_at=STAMP,
                account_id_hash="a"*64, source_hash="b"*64)))
        records.append(("ExplorationFeedback", dict(lot_id="lot", observation_id="observation",
            predictor_fit=False, synthetic=False, actual_fees="0", net_pnl="2",
            recorded_at=STAMP, secret="never-export-this")))
        for i, (kind, doc) in enumerate(records):
            raw = json.dumps(doc, sort_keys=True)
            db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)",
                       ("experiment", str(i), kind, STAMP, raw,
                        hashlib.sha256(raw.encode()).hexdigest()))
    return ops, copy, exp


def config(tmp_path):
    CoreConfig, _, _ = api()
    ops, copy, exp = fixture_stores(tmp_path)
    return CoreConfig(operational_db=ops, learner_db=copy, experiment_db=exp,
                      account_hash="a"*64)


def signature(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def test_copied_ledger_is_not_a_second_trade_and_reads_do_not_change_newer_schema(tmp_path):
    cfg = config(tmp_path)
    _, inspect, trace = api()
    paths = (cfg.operational_db, cfg.learner_db, cfg.experiment_db)
    before = [signature(p) for p in paths]
    first = inspect(cfg, as_of=AS_OF)
    second = inspect(cfg, as_of=AS_OF)
    assert first.to_dict() == second.to_dict()
    assert first.to_dict()["fill_summary"] == {
        "authoritative": 2, "copied_duplicates": 2, "copy_only": 0, "conflicts": 0}
    bundle = trace(first, "lot").to_dict()
    assert len(bundle["fills"]) == 2
    assert bundle["gross_pnl"] == "2"
    assert bundle["verified_net_pnl"] is None
    assert {"COST_PROVENANCE_UNKNOWN", "NO_LINKED_PARAMETER_UPDATE"} <= set(bundle["gaps"])
    assert bundle["stages"]["protection"] and bundle["stages"]["flat"]
    assert [signature(p) for p in paths] == before
    assert not list(tmp_path.glob("*-journal"))
    with sqlite3.connect(cfg.operational_db) as db:
        assert db.execute("SELECT version FROM schema_version").fetchone() == (9999,)
        assert db.execute("SELECT count(*) FROM fills").fetchone() == (2,)
    serialized = json.dumps(first.to_dict()) + json.dumps(bundle)
    assert "secret-account-number" not in serialized
    assert "never-export-this" not in serialized


def test_stale_deployment_file_never_overrides_recorded_runtime(tmp_path):
    cfg = config(tmp_path)
    CoreConfig, inspect, _ = api()
    deployment = tmp_path / "deployment.json"
    deployment.write_text(json.dumps({"console_commit": "a"*40, "token": "never-export-this"}))
    cfg = CoreConfig(**{**cfg.__dict__, "deployment_json": deployment})
    doc = inspect(cfg, as_of=AS_OF).to_dict()
    assert "DEPLOYMENT_RELEASE_CONFLICT:console" in doc["gaps"]
    assert doc["runtime"][0]["git_commit"] == "b"*40
    assert doc["broker_freshness"] == "NOT_PROBED"
    assert "never-export-this" not in json.dumps(doc)


def test_missing_store_is_a_gap_and_is_not_created(tmp_path):
    CoreConfig, inspect, trace = api()
    missing = tmp_path / "missing.db"
    cfg = CoreConfig(operational_db=missing, learner_db=missing, experiment_db=missing,
                     account_hash="a"*64)
    doc = inspect(cfg, as_of=AS_OF)
    assert "STORE_MISSING:operations" in doc.to_dict()["gaps"]
    assert "LOT_NOT_FOUND" in trace(doc, "absent").to_dict()["gaps"]
    assert not missing.exists()


def test_conflicting_copy_is_reported_without_overwriting_authority(tmp_path):
    cfg = config(tmp_path)
    _, inspect, trace = api()
    with sqlite3.connect(cfg.learner_db) as db:
        db.execute("UPDATE fills SET price='999' WHERE fill_id='buy'")
    snapshot = inspect(cfg, as_of=AS_OF)
    assert snapshot.to_dict()["fill_summary"]["conflicts"] == 1
    assert "COPIED_FILL_CONFLICT:buy" in snapshot.to_dict()["gaps"]
    assert trace(snapshot, "lot").to_dict()["gross_pnl"] == "2"


def test_cutoff_uses_instants_and_missing_observation_is_explicit(tmp_path):
    cfg = config(tmp_path)
    _, inspect, trace = api()
    with sqlite3.connect(cfg.operational_db) as db:
        db.execute("INSERT INTO fills VALUES('future','x','XLI','BUY','1','900',"
                   "'2026-10-14T00:30:00-04:00','0')")
    snapshot = inspect(cfg, as_of=AS_OF)
    assert snapshot.to_dict()["fill_summary"]["authoritative"] == 2
    assert "OBSERVATION_MISSING" in trace(snapshot, "lot").to_dict()["gaps"]
    with pytest.raises(ValueError, match="timezone"):
        inspect(cfg, as_of=datetime(2026, 10, 14))


def test_tampered_event_cannot_be_used_as_trade_evidence(tmp_path):
    cfg = config(tmp_path)
    _, inspect, trace = api()
    with sqlite3.connect(cfg.experiment_db) as db:
        db.execute("UPDATE events SET document=replace(document,'RECONCILED_FLAT','FAKE_FLAT')"
                   " WHERE document LIKE '%RECONCILED_FLAT%'")
    snapshot = inspect(cfg, as_of=AS_OF)
    assert "EVENT_DIGEST_MISMATCH" in snapshot.to_dict()["gaps"]
    assert not trace(snapshot, "lot").to_dict()["stages"]["flat"]


def test_task_pin_exposes_stale_deployment_claim_and_future_metadata_is_rejected(tmp_path):
    cfg = config(tmp_path)
    CoreConfig, inspect, _ = api()
    deployment = tmp_path / "deployment.json"
    host = tmp_path / "host.json"
    deployment.write_text(json.dumps({"console_commit": "a"*40}))
    payload = {"observed_at": "2026-10-13T23:59:00Z",
               "tasks": [{"name": "Elrond Console", "state": "Running", "release": "b"*40}],
               "processes": []}
    host.write_text(json.dumps(payload))
    cfg = CoreConfig(**{**cfg.__dict__, "deployment_json": deployment, "runtime_metadata": host})
    assert "TASK_DEPLOYMENT_CONFLICT:console" in inspect(cfg, as_of=AS_OF).to_dict()["gaps"]
    payload["observed_at"] = "2026-10-14T01:00:00Z"
    host.write_text(json.dumps(payload))
    doc = inspect(cfg, as_of=AS_OF).to_dict()
    assert "HOST_METADATA_OUTSIDE_CUTOFF" in doc["gaps"]
    assert not doc["host_metadata"]["tasks"]


def test_cli_reads_wal_and_marks_missing_artifact_without_writing_source(tmp_path):
    cfg = config(tmp_path)
    with sqlite3.connect(cfg.operational_db) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO fills VALUES('wal-fill','w','XLI','BUY','1','10',"
                       "'2026-10-13T12:00:00Z','0')")
        writer.commit()
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({**cfg.__dict__, "deployment_json": tmp_path/'absent.json'},
                                       default=str))
        before = signature(cfg.operational_db)
        process = subprocess.run([sys.executable, "-I", "-B", "-m", "quantbot.paper_core.inventory",
            "--config", str(manifest), "--as-of", AS_OF.isoformat(), "--lot", "lot"],
            capture_output=True, text=True, check=True)
        doc = json.loads(process.stdout)
        assert doc["inventory"]["fill_summary"]["authoritative"] == 3
        assert "ARTIFACT_MISSING_OR_INVALID:absent.json" in doc["inventory"]["gaps"]
        assert signature(cfg.operational_db) == before
        assert doc["lots"][0]["gross_pnl"] == "2"


def test_future_occurrence_cannot_supply_flat_evidence(tmp_path):
    cfg = config(tmp_path)
    _, inspect, trace = api()
    with sqlite3.connect(cfg.experiment_db) as db:
        row = db.execute("SELECT idempotency_key,document FROM events "
                         "WHERE document LIKE '%RECONCILED_FLAT%'").fetchone()
        doc = json.loads(row[1])
        doc["occurred_at"] = "2026-10-20T12:00:00Z"
        raw = json.dumps(doc)
        db.execute("UPDATE events SET document=?,digest=? WHERE idempotency_key=?",
                   (raw, hashlib.sha256(raw.encode()).hexdigest(), row[0]))
    snapshot = inspect(cfg, as_of=AS_OF)
    assert not trace(snapshot, "lot").to_dict()["stages"]["flat"]
    assert "INVALID_EVENT:LotEvent" in snapshot.to_dict()["gaps"]


def test_standalone_bundle_retains_account_and_recorded_entry_wait(tmp_path):
    cfg = config(tmp_path)
    CoreConfig, inspect, trace = api()
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"recorded_at": "2026-10-13T23:59:00Z",
        "status": "NORMAL_WAIT", "reason": "MARKET_CLOSED", "code_commit": "c"*40,
        "unexpected": "never-export-this"}))
    assert "entry_receipt" in CoreConfig.__dataclass_fields__, "entry receipt not inspected"
    cfg = CoreConfig(**{**cfg.__dict__, "entry_receipt": receipt})
    with sqlite3.connect(cfg.operational_db) as db:
        db.execute("UPDATE kill_switch_state SET engaged=0")
    snapshot = inspect(cfg, as_of=AS_OF)
    doc = snapshot.to_dict()
    assert doc["first_blocker"] == "MARKET_CLOSED"
    assert doc["entry_state"]["age_seconds"] == 60
    assert doc["broker_freshness"] == "NOT_PROBED"
    bundle = trace(snapshot, "lot").to_dict()
    assert bundle["account_hash"] == "a"*64
    assert "never-export-this" not in json.dumps(doc)
    with sqlite3.connect(cfg.operational_db) as db:
        db.execute("UPDATE kill_switch_state SET engaged=1")
    assert inspect(cfg, as_of=AS_OF).to_dict()["first_blocker"] == "RECORDED_STOP_ENGAGED"


@pytest.mark.parametrize("account", [None, "b"*64])
def test_accountless_or_mismatched_lot_evidence_is_rejected(tmp_path, account):
    cfg = config(tmp_path)
    _, inspect, trace = api()
    with sqlite3.connect(cfg.experiment_db) as db:
        for key, raw in db.execute(
                "SELECT idempotency_key,document FROM events WHERE kind='LotEvent'"):
            doc = json.loads(raw)
            if account is None:
                doc.pop("account_id_hash")
            else:
                doc["account_id_hash"] = account
            raw = json.dumps(doc)
            db.execute("UPDATE events SET document=?,digest=? WHERE idempotency_key=?",
                       (raw, hashlib.sha256(raw.encode()).hexdigest(), key))
    snapshot = inspect(cfg, as_of=AS_OF)
    assert "EVENT_ACCOUNT_MISMATCH" in snapshot.to_dict()["gaps"]
    assert "LOT_NOT_FOUND" in trace(snapshot, "lot").to_dict()["gaps"]


@pytest.mark.parametrize("tasks", [None, [None], "unexpected"])
def test_malformed_host_collections_do_not_discard_store_evidence(tmp_path, tasks):
    cfg = config(tmp_path)
    CoreConfig, inspect, _ = api()
    host = tmp_path / "host.json"
    host.write_text(json.dumps({"observed_at": STAMP, "tasks": tasks, "processes": []}))
    cfg = CoreConfig(**{**cfg.__dict__, "runtime_metadata": host})
    doc = inspect(cfg, as_of=AS_OF).to_dict()
    assert "HOST_METADATA_COLLECTION_INVALID:tasks" in doc["gaps"]
    assert doc["fill_summary"]["authoritative"] == 2
