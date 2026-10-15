"""M6 release admission, independent readiness and populated history compatibility."""

import gzip
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quantbot.paper_core import runtime


def test_release_preparation_is_supported():
    assert hasattr(runtime, "prepare_release"), "M6 needs a side-effect-free pinned release API"


def test_forward_model_rollback_is_explicit_supported_command():
    from quantbot.paper_core.__main__ import parser

    args = parser().parse_args(
        ["--config", str(Path.cwd() / "prepared.json"), "--command", "rollback-model"]
    )
    assert args.command == "rollback-model"


def test_operational_lane_alone_cannot_authorize_adapter():
    assert hasattr(runtime, "require_runtime_broker"), "broker admission needs durable authority"


def test_runtime_acknowledgement_uses_current_immutable_intent(tmp_path):
    import asyncio

    from tests.fixtures.core_m3 import prepare

    from quantbot.domain import IntentState
    from quantbot.paper_core.runtime import CoreExecutionCoordinator
    from quantbot.storage import StorageRepository

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    # Use the existing initialized fixture's V2 production evaluation for a real claim.
    from quantbot.paper_core.trader import trader_cycle

    asyncio.run(trader_cycle(store, broker, observation, model, proof, now=clocks.wall))
    link = next(r for r in store.links() if r["role"] == "ENTRY")
    with db.transaction() as session:
        repo = StorageRepository(session)
        intent = repo.get_order_intent(link["intent_id"])
        order = repo.get_broker_order_by_client_order_id(intent.client_order_id)
    coordinator = CoreExecutionCoordinator(
        store, broker, runtime.fixture_coordinator(store, broker)._settings
    )
    stale = intent.model_copy(update={"state": IntentState.SUBMITTING})
    assert coordinator._adopt(stale, order).state is IntentState.FILLED
    assert len(broker.posts) == 1
    with pytest.raises(Exception, match="changed"):
        coordinator._adopt(stale.model_copy(update={"client_order_id": "conflict"}), order)
    db.close()


def test_populated_v34_capture_has_real_activation_and_claim():
    rows = json.loads(
        gzip.decompress((Path(__file__).parents[2] / "fixtures/core_v34_m5.json.gz").read_bytes())
    )
    assert rows["schema_version"][0]["version"] == 34
    assert len(rows["core_round_trips"]) == 40
    pointer = rows["core_active_model"][0]
    event = next(
        json.loads(r["payload"])
        for r in rows["core_activations"]
        if r["id"] == pointer["activation_id"]
    )
    assert event["authority_id"] and event["receipt_id"]
    assert any(
        json.loads(r["payload"]).get("activation_id") == pointer["activation_id"]
        for r in rows["core_decisions"]
    )


def test_populated_v34_migration_preserves_ids_omissions_claim_and_owner(tmp_path):
    from alembic import command
    from sqlalchemy import create_engine

    from quantbot.paper_core.activation import ActivationAuthority, active_in_session
    from quantbot.paper_core.contracts import (
        CoreConfig,
        Decision,
        ModelArtifact,
        ProtectionPlan,
        ReconciliationProof,
    )
    from quantbot.paper_core.store import CoreStore
    from quantbot.storage import Database, MigrationRequiredError

    fixture = json.loads(
        gzip.decompress((Path(__file__).parents[2] / "fixtures/core_v34_m5.json.gz").read_bytes())
    )
    path = tmp_path / "prior.db"
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        command.upgrade(Database._alembic_config(connection), "0034")
        connection.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
        for name, rows in fixture.items():
            if not rows or name in {"schema_version", "alembic_version"}:
                continue
            if name == "kill_switch_state":
                connection.exec_driver_sql("DELETE FROM kill_switch_state")
            for row in rows:
                keys = tuple(row)
                connection.exec_driver_sql(
                    f'INSERT INTO "{name}" ({",".join(keys)}) '
                    f"VALUES ({','.join('?' for _ in keys)})",
                    tuple(row[k] for k in keys),
                )
    engine.dispose()
    before = path.read_bytes()
    with pytest.raises(MigrationRequiredError):
        Database(path)
    assert path.read_bytes() == before
    with Database.migrate(path) as db:
        config = CoreConfig.model_validate_json(fixture["core_identities"][0]["payload"])
        store = CoreStore(db, config)
        with db.transaction() as session:
            selection = active_in_session(store, session)
            assert selection[1].id == fixture["core_active_model"][0]["activation_id"]
        for name, kind in (
            ("core_decisions", Decision),
            ("core_models", ModelArtifact),
            ("core_protection_plans", ProtectionPlan),
            ("core_activation_authorities", ActivationAuthority),
            ("core_reconciliation_proofs", ReconciliationProof),
        ):
            for row in fixture[name]:
                assert kind.model_validate_json(row["payload"]).id == row["id"]
                if kind is ReconciliationProof:
                    assert kind.model_validate_json(row["payload"]).ledger_state_id is None
        with db.engine.connect() as connection:
            for name, rows in fixture.items():
                if name in {"schema_version", "alembic_version"}:
                    continue
                actual = [
                    dict(r)
                    for r in connection.exec_driver_sql(f'SELECT * FROM "{name}"').mappings()
                ]
                assert sorted(actual, key=lambda r: json.dumps(r, sort_keys=True)) == sorted(
                    rows, key=lambda r: json.dumps(r, sort_keys=True)
                )
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        print(
            "M6_V34_COMPATIBILITY:"
            + json.dumps(
                {
                    "schema": 35,
                    "old_sha256": hashlib.sha256(before).hexdigest(),
                    "active_event": selection[1].id,
                    "claimed_decisions": len(fixture["core_decisions"]),
                    "protection_plans": len(fixture["core_protection_plans"]),
                    "all_prior_rows_equal": True,
                }
            )
        )


def test_cli_status_must_not_migrate_or_write(tmp_path):
    assert hasattr(runtime, "read_status"), "status must be explicitly read-only"
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_version(version INTEGER)")
        connection.execute("INSERT INTO schema_version VALUES (34)")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="schema"):
        runtime.read_status(path)
    assert path.read_bytes() == before


def test_fixture_model_bootstrap_cannot_commission_paper(tmp_path):
    from datetime import UTC, datetime

    from quantbot.paper_core.activation import initialize_fixture_model
    from quantbot.paper_core.contracts import CoreConfig, ModelArtifact
    from quantbot.paper_core.store import CoreStore
    from quantbot.storage import Database, StateConflictError

    with Database.create(tmp_path / "core.db") as db:
        store = CoreStore(
            db, CoreConfig(account_id="paper", symbol="XLE", evidence_lane="BROKER_PAPER")
        )
        with pytest.raises(StateConflictError, match="FIXTURE"):
            initialize_fixture_model(store, ModelArtifact(), at=datetime.now(UTC))


def make_process_release(tmp_path):
    from quantbot.domain import Account
    from quantbot.paper_core import activation
    from quantbot.paper_core.__main__ import derived_core
    from quantbot.paper_core.contracts import CoreConfig
    from quantbot.paper_core.release import RuntimeConfig
    from quantbot.paper_core.simulation import SyntheticBroker
    from quantbot.paper_core.store import CoreStore
    from quantbot.paper_core.time_evidence import SystemClocks
    from quantbot.storage import Database

    db = Database.create(tmp_path / "core.db")
    root = Path(__file__).resolve().parents[3]
    config = RuntimeConfig(
        repository=str(root),
        store_path=str(db.path),
        store_identity=db.store_identity,
        expected_account_hash=hashlib.sha256(b"synthetic").hexdigest(),
        core=CoreConfig(
            account_id="synthetic",
            symbol="XLE",
            release_id="UNBOUND_PREPARED_RELEASE",
            clock_profile="PAPER_TIME_V2",
        ),
        holdout_source=str(tmp_path / "canonical.db"),
        learning_origin=datetime.now(UTC) - timedelta(days=4),
        guardian_poll_seconds=0.2,
        outcomes_poll_seconds=0.2,
        entry_poll_seconds=0.2,
        submission_enabled=True,
    )
    release = runtime.prepare_release(config)
    clocks = SystemClocks()
    store = CoreStore(db, derived_core(release, "synthetic"), clocks=clocks)
    broker = SyntheticBroker(
        account_id="synthetic",
        now=clocks.sample()[0],
        initial_account=Account(
            account_id="synthetic", cash=100000, equity=100000, buying_power=100000, currency="USD"
        ),
    )
    runtime.initialize_fixture(store, broker, now=clocks.sample()[0], entry_enabled=True)
    activation.initialize_fixture_model(store, config.initial_model, at=clocks.sample()[0])
    config_path, manifest_path = tmp_path / "config.json", tmp_path / "manifest.json"
    config_path.write_text(config.model_dump_json())
    manifest_path.write_text(release.model_dump_json())
    return db, store, config, release, config_path, manifest_path


def test_release_identity_detects_extra_executable_and_mutation(tmp_path):
    db, store, config, release, *_ = make_process_release(tmp_path)
    try:
        assert runtime.verify_release(release)["objections"] == []
        changed = release.model_copy(update={"executable_pins": release.executable_pins[:-1]})
        assert "EXECUTABLE_INVENTORY_MISMATCH" in runtime.verify_release(changed)["objections"]
        assert store.config.release_id == release.id
        assert store.config_id != config.id
    finally:
        db.close()


def test_default_config_and_release_identity_survive_json_reload(tmp_path):
    from quantbot.paper_core.release import PreparedRelease, RuntimeConfig

    db, _, config, _, *_ = make_process_release(tmp_path)
    try:
        default = RuntimeConfig.model_validate(
            config.model_dump(
                exclude={"guardian_poll_seconds", "outcomes_poll_seconds", "entry_poll_seconds"}
            )
        )
        reloaded_config = RuntimeConfig.model_validate_json(default.model_dump_json())
        assert reloaded_config.id == default.id
        release = runtime.prepare_release(default)
        reloaded = PreparedRelease.model_validate_json(release.model_dump_json())
        assert reloaded.id == release.id
        assert reloaded.config.id == release.config.id
        assert not runtime.verify_release(reloaded)["objections"]
    finally:
        db.close()


def test_actual_three_role_processes_restart_fence_and_continue_outcomes(tmp_path):
    from sqlalchemy import select

    from quantbot.paper_core.authority import guardian_objections
    from quantbot.paper_core.contracts import GuardianReceipt
    from quantbot.paper_core.store import table
    from quantbot.paper_core.time_evidence import TimeEvidence, evidence_objections

    db, store, config, release, config_path, manifest_path = make_process_release(tmp_path)
    import asyncio

    from tests.fixtures.core_m6 import broker as fixture_broker

    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import trader_cycle

    close_at = store.clocks.sample()[0] + timedelta(seconds=121.5)
    (tmp_path / "session-close.json").write_text(json.dumps(close_at.isoformat()))
    seeded = fixture_broker(store, config)

    async def seed_owned():
        await guardian_cycle(store, seeded, now=store.clocks.sample()[0])
        observation = await seeded.observation()
        decision = await trader_cycle(
            store,
            seeded,
            observation,
            config.initial_model,
            store.latest_proof(),
            now=store.clocks.sample()[0],
        )
        assert seeded.posts and decision.action == "BUY", decision.gates
        return decision

    owned = asyncio.run(seed_owned())
    (tmp_path / "clock-failure").write_text("explicit trader-only failure")
    processes, logs = [], []

    def start(role, cycles):
        log = (tmp_path / (role + str(len(processes)) + ".txt")).open("w")
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "quantbot.paper_core",
                "--config",
                str(config_path),
                "--manifest",
                str(manifest_path),
                "--role",
                role,
                "--cycles",
                str(cycles),
                "--fixture-driver",
                "tests.fixtures.core_m6:broker",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=Path(config.repository),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        processes.append(process)
        logs.append(log)
        return process

    try:
        guardian = start("guardian", 8)
        outcomes = start("outcomes", 25)
        trader = start("trader", 3)
        for process in (guardian, trader):
            process.wait(timeout=60)
            assert process.returncode == 0
        time.sleep(0.5)
        store.runtime_role = "trader"
        with db.transaction() as session:
            assert guardian_objections(store, session, now=store.clocks.sample()[0]) == (
                "CORE_INDEPENDENT_GUARDIAN_NOT_READY",
            )
        restarted = start("guardian", 3)
        restarted.wait(timeout=60)
        outcomes.wait(timeout=60)
        assert restarted.returncode == outcomes.returncode == 0
        status = runtime.read_status(db.path)
        assert len({r["pid"] for r in status["roles"].values()}) == 3
        assert status["roles"]["outcomes"]["last_useful_progress"]
        assert status["roles"]["guardian"]["pid"] != guardian.pid
        assert store.documents("core_round_trips")
        assert store.documents("core_round_trips")[0]["decision_id"] == owned.id
        from quantbot.paper_core.release import read_export

        before_export = db.path.read_bytes()
        joined = read_export(db.path, owned.id)
        assert db.path.read_bytes() == before_export
        assert joined["observation"] and joined["model"] and joined["activation"]
        assert len(joined["fills"]) == 2 and len(joined["orders"]) == 2
        assert joined["guardian_receipts"] and joined["reconciliation_proofs"]
        from decimal import Decimal

        assert Decimal(joined["current_outcome"]["open_quantity"]) == 0
        print("M6_JOINED_PROCESS:" + json.dumps(joined))
        with db.transaction() as session:
            times = [
                TimeEvidence.model_validate_json(p)
                for p in session.execute(select(table("core_time_evidence").c.payload)).scalars()
            ]
            receipts = [
                GuardianReceipt.model_validate_json(p)
                for p in session.execute(
                    select(table("core_guardian_receipts").c.payload)
                ).scalars()
            ]
        assert times and len({e.process_id for e in times}) >= 3
        assert "TIME_PROCESS_CHANGED" in evidence_objections(
            next(e for e in times if e.process_id != store.clocks.process_id), store.clocks
        )
        assert any(r.protection_ids for r in receipts)
        probe = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                """
import json,os,sys
from pathlib import Path
from quantbot.storage import Database,StorageRepository
from quantbot.paper_core.store import CoreStore
from quantbot.paper_core.contracts import CoreConfig,Decision,ProtectionPlan
from quantbot.paper_core.time_evidence import TimeEvidence,evidence_objections
from quantbot.paper_core.trader import claim_entry
from quantbot.paper_core.send_guard import final_send_objections
from quantbot.risk import OrderPurpose
with Database(Path(sys.argv[1])) as db:
 with db.engine.connect() as c:
  payload=c.exec_driver_sql('SELECT payload FROM core_identities').scalar_one()
  cfg=CoreConfig.model_validate_json(payload)
 store=CoreStore(db,cfg)
 link=next(r for r in store.links() if r['role']=='ENTRY')
 decision=store.read('core_decisions',link['decision_id'],Decision)
 plan=store.read('core_protection_plans',link['protection_id'],ProtectionPlan)
 proof=store.read('core_time_evidence',decision.time_evidence_id,TimeEvidence)
 with db.transaction() as s: intent=StorageRepository(s).get_order_intent(link['intent_id'])
 result={'pid':os.getpid(),'epoch':store.clocks.process_id,'original_epoch':proof.process_id,
 'time_objections':evidence_objections(proof,store.clocks),
 'claim_refused':claim_entry(store,decision,plan) is None,
 'send_objections':final_send_objections(store,intent,OrderPurpose.ENTRY,clock=lambda:store.clocks.sample()[0])}
 print(json.dumps(result))
""",
                str(db.path),
            ],
            text=True,
            capture_output=True,
            cwd=Path(config.repository),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=15,
        )
        assert probe.returncode == 0, probe.stderr
        restart_probe = json.loads(probe.stdout)
        assert restart_probe["claim_refused"]
        assert "TIME_PROCESS_CHANGED" in restart_probe["send_objections"]
        print("M6_RESTART_SEND:" + json.dumps(restart_probe))
        assert all(r.protection_ids == () or r.source_refs == r.protection_ids for r in receipts)
        print(
            "M6_PROCESSES:"
            + json.dumps(
                {
                    "status": status,
                    "release_id": release.id,
                    "pids": [p.pid for p in processes],
                    "time_epochs": sorted({e.process_id for e in times}),
                    "restarted_proof_objections": evidence_objections(
                        next(e for e in times if e.process_id != store.clocks.process_id),
                        store.clocks,
                    ),
                }
            )
        )
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        for log in logs:
            log.close()
        for file in tmp_path.glob("*.txt"):
            print("M6_PROCESS_LOG:" + file.name + ":" + file.read_text(errors="replace"))
        db.close()


def test_flat_runtime_independent_guardian_ack_submits_once_and_protects(tmp_path):
    import time
    from decimal import Decimal

    from quantbot.paper_core.release import read_export

    db, store, config, release, config_path, manifest_path = make_process_release(tmp_path)
    assert not store.links()
    (tmp_path / "session-close.json").write_text(
        json.dumps((store.clocks.sample()[0] + timedelta(seconds=128)).isoformat())
    )
    processes, streams = [], []

    def start(role, cycles):
        stream = (tmp_path / ("flat-" + role + ".txt")).open("w")
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "quantbot.paper_core",
                "--config",
                str(config_path),
                "--manifest",
                str(manifest_path),
                "--role",
                role,
                "--cycles",
                str(cycles),
                "--fixture-driver",
                "tests.fixtures.core_m6:broker",
            ],
            stdout=stream,
            stderr=subprocess.STDOUT,
            cwd=Path(config.repository),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        processes.append(process)
        streams.append(stream)
        return process

    try:
        start("guardian", 70)
        start("outcomes", 75)
        deadline = time.monotonic() + 10
        while "guardian" not in runtime.read_status(db.path)["roles"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        start("trader", 20)
        for process in processes:
            process.wait(timeout=60)
            assert process.returncode == 0
        for stream in streams:
            stream.close()
        posts_file = tmp_path / "process-posts.jsonl"
        posts = (
            [json.loads(s) for s in posts_file.read_text().splitlines()]
            if posts_file.exists()
            else []
        )
        assert sum(p["side"] == "BUY" for p in posts) == 1, posts
        assert sum(p["side"] == "SELL" for p in posts) == 1, posts
        entry = next(link for link in store.links() if link["role"] == "ENTRY")
        joined = read_export(db.path, entry["decision_id"])
        assert Decimal(joined["current_outcome"]["open_quantity"]) == 0
        assert len(joined["fills"]) == 2
        assert any(
            entry["protection_id"] in r["protection_ids"]
            for r in joined["guardian_receipts"].values()
        )
        status = runtime.read_status(db.path)
        assert len({r["pid"] for r in status["roles"].values()}) == 3
        assert posts[0]["pid"] == status["roles"]["trader"]["pid"]
        assert posts[1]["pid"] == status["roles"]["guardian"]["pid"]
        print(
            "M6_FIX1_FLAT_HANDSHAKE:"
            + json.dumps(
                {
                    "release_id": release.id,
                    "posts": posts,
                    "actual_role_status": status,
                    "joined": joined,
                }
            )
        )
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        for stream in streams:
            stream.close()
        db.close()


@pytest.mark.parametrize(
    "change",
    [
        "refresh",
        "marks",
        "low_bp",
        "low_equity",
        "cash",
        "quantity",
        "order",
        "reservation",
        "revoked",
        "expired_decision",
        "expired_plan",
        "expired_guardian",
    ],
)
@pytest.mark.parametrize("phase", ["claim", "send"])
def test_runtime_locked_claim_accepts_only_safe_successor(tmp_path, change, phase):
    import os
    from decimal import Decimal
    from types import SimpleNamespace

    from tests.fixtures.core_m2 import NOW, prepare

    from quantbot.domain import (
        BrokerOrder,
        IntentState,
        OrderIntent,
        OrderSide,
        OrderType,
        TimeInForce,
    )
    from quantbot.paper_core.authority import RoleHeartbeat, publish_heartbeat
    from quantbot.paper_core.contracts import ProtectionPlan
    from quantbot.paper_core.guardian import acknowledge_handoffs
    from quantbot.paper_core.store import certify_inventory, put, table
    from quantbot.paper_core.trader import claim_entry, evaluate_entry, record_observation
    from quantbot.storage import StorageRepository

    db, store, broker, original, observation, model = prepare(tmp_path)
    try:
        if change == "expired_plan":
            observation = observation.model_copy(
                update={"session_close": NOW + timedelta(seconds=121)}
            )
        decision = evaluate_entry(store.config, observation, model, original)
        assert decision.action == "BUY"
        plan = ProtectionPlan.for_decision(decision, store.config)
        record_observation(store, observation)
        earlier_claim = None
        if phase == "send":
            at = NOW + timedelta(milliseconds=1)
            store.clocks = SimpleNamespace(
                sample=lambda: (at, 1.0), process_id="deterministic-trader"
            )
            store.runtime_role = "guardian"
            initial_receipt = acknowledge_handoffs(store, now=at)
            publish_heartbeat(
                store,
                RoleHeartbeat(
                    role="guardian",
                    release_id=store.config.release_id,
                    config_id=store.config_id,
                    store_identity=db.store_identity,
                    owner=store.config.owner,
                    generation=store.config.generation,
                    process_epoch="deterministic-other-guardian",
                    pid=os.getpid() + 100000,
                    recorded_at=at,
                    valid_until=at + timedelta(seconds=10),
                    last_useful_progress=at,
                    objections=(),
                    evidence_ids=(initial_receipt.id,),
                    proof_age_seconds=0,
                    guardian_receipt_id=initial_receipt.id,
                ),
            )
            store.runtime_role = "trader"
            earlier_claim = claim_entry(store, decision, plan)
            assert earlier_claim is not None
            with db.transaction() as session:
                StorageRepository(session).transition_order_intent(
                    earlier_claim.intent.intent_id,
                    IntentState.ORDER_CREATED,
                    expected_current=IntentState.RISK_APPROVED,
                )
        now = NOW + timedelta(
            seconds=120 if change == "expired_decision" else 2 if change == "expired_plan" else 1
        )
        store.clocks = SimpleNamespace(sample=lambda: (now, 1.0), process_id="deterministic-trader")
        if change == "marks":
            broker.set_mark("SPY", Decimal(120), as_of=now)
        if change in {"cash", "quantity"}:
            with db.transaction() as session:
                put(
                    session,
                    "core_external_owner_events",
                    "explicit-economic-change",
                    {
                        "event_id": "explicit-economic-change",
                        "symbol": "SPY" if change == "quantity" else "",
                        "owner": "FIXTURE_LEGACY",
                        "quantity_delta": "1" if change == "quantity" else "0",
                        "net_cash": "-1" if change == "cash" else "0",
                        "account_id": broker.account_id,
                    },
                )
            if change == "cash":
                broker.cash -= 1
            else:
                broker.quantities["SPY"] += 1
        if change in {"order", "reservation"}:
            extra = OrderIntent(
                intent_id="extra",
                client_order_id="extra-client",
                strategy_id="FIXTURE_LEGACY",
                signal_date=NOW.date(),
                symbol="SPY",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
                quantity=1,
                created_at=now,
            )
            with db.transaction() as session:
                repo = StorageRepository(session)
                repo.create_order_intent(extra)
                if change == "order":
                    repo.save_broker_order(
                        BrokerOrder(
                            broker_order_id="extra-order",
                            client_order_id=extra.client_order_id,
                            symbol=extra.symbol,
                            side=extra.side,
                            order_type=extra.order_type,
                            time_in_force=extra.time_in_force,
                            quantity=1,
                            filled_quantity=0,
                            status="cancelled",
                            submitted_at=now,
                        )
                    )
                    repo.transition_order_intent(
                        extra.intent_id,
                        IntentState.ORDER_CREATED,
                        expected_current=IntentState.RISK_APPROVED,
                    )
                    repo.transition_order_intent(
                        extra.intent_id,
                        IntentState.SUBMITTING,
                        expected_current=IntentState.ORDER_CREATED,
                    )
                    repo.transition_order_intent(
                        extra.intent_id,
                        IntentState.BROKER_ACCEPTED,
                        expected_current=IntentState.SUBMITTING,
                    )
                    repo.transition_order_intent(
                        extra.intent_id,
                        IntentState.CANCELLED,
                        expected_current=IntentState.BROKER_ACCEPTED,
                    )
        account = broker.snapshot().account
        if change == "low_bp":
            account = account.model_copy(update={"buying_power": Decimal(100)})
        if change == "low_equity":
            account = account.model_copy(update={"equity": Decimal(1000)})
        fresh = store.proof(
            account,
            tuple(sorted(broker.quantities.items())),
            now=now,
            positions=broker.position_snapshot(),
            acquisition_id="fresh-poll-does-not-change-economics",
        )
        certify_inventory(store, fresh)
        if change == "revoked":
            with db.transaction() as session:
                session.execute(
                    table("core_cash_authority_revocations")
                    .insert()
                    .values(
                        snapshot_id="core-certified-" + original.id,
                        reason="EXPLICIT_OLD_AUTHORITY_REVOKED",
                    )
                )
        store.runtime_role = "guardian"
        receipt = acknowledge_handoffs(store, now=now)
        publish_heartbeat(
            store,
            RoleHeartbeat(
                role="guardian",
                release_id=store.config.release_id,
                config_id=store.config_id,
                store_identity=db.store_identity,
                owner=store.config.owner,
                generation=store.config.generation,
                process_epoch="deterministic-other-guardian",
                pid=os.getpid() + 100000,
                recorded_at=now,
                valid_until=now if change == "expired_guardian" else now + timedelta(seconds=10),
                last_useful_progress=now,
                objections=(),
                evidence_ids=(receipt.id,),
                proof_age_seconds=0,
                guardian_receipt_id=receipt.id,
            ),
        )
        store.runtime_role = "trader"
        claimed = None if phase == "send" else claim_entry(store, decision, plan)
        if phase == "send":
            from quantbot.paper_core.send_guard import final_send_objections
            from quantbot.risk import OrderPurpose

            with db.transaction() as session:
                selected = StorageRepository(session).get_order_intent(
                    earlier_claim.intent.intent_id
                )
            objections = final_send_objections(
                store, selected, OrderPurpose.ENTRY, clock=lambda: now
            )
            assert (not objections) == (change in {"refresh", "marks"}), objections
        else:
            assert (claimed is not None) == (change in {"refresh", "marks"})
        assert not broker.posts
        if claimed is not None:
            assert claimed.decision_id == decision.id and claimed.protection_id == plan.id
            from quantbot.paper_core.send_guard import final_send_objections
            from quantbot.risk import OrderPurpose

            store.runtime_role = "guardian"
            receipt = acknowledge_handoffs(store, now=now)
            publish_heartbeat(
                store,
                RoleHeartbeat(
                    role="guardian",
                    release_id=store.config.release_id,
                    config_id=store.config_id,
                    store_identity=db.store_identity,
                    owner=store.config.owner,
                    generation=store.config.generation,
                    process_epoch="deterministic-other-guardian",
                    pid=os.getpid() + 100000,
                    recorded_at=now,
                    valid_until=now + timedelta(seconds=10),
                    last_useful_progress=now,
                    objections=(),
                    evidence_ids=(receipt.id,),
                    proof_age_seconds=0,
                    guardian_receipt_id=receipt.id,
                ),
            )
            with db.transaction() as session:
                intent = StorageRepository(session).transition_order_intent(
                    claimed.intent.intent_id,
                    IntentState.ORDER_CREATED,
                    expected_current=IntentState.RISK_APPROVED,
                )
            store.runtime_role = "trader"
            assert final_send_objections(store, intent, OrderPurpose.ENTRY, clock=lambda: now) == ()
        print(
            "M6_FIX1_SUCCESSOR:"
            + json.dumps(
                {
                    "change": change,
                    "phase": phase,
                    "claimed": claimed is not None,
                    "decision_id": decision.id,
                    "original_proof_id": original.id,
                    "current_proof_id": fresh.id,
                    "ledger_before": original.ledger_state_id,
                    "ledger_after": fresh.ledger_state_id,
                }
            )
        )
    finally:
        db.close()
