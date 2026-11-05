"""Accepted whole-branch repair regressions through production entry points."""

import asyncio
import gzip
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event, select

from quantbot.paper_core import outcomes
from quantbot.paper_core.__main__ import derived_core
from quantbot.paper_core.activation import initialize_operational_model
from quantbot.paper_core.authority import RuntimeCommission, commission_runtime
from quantbot.paper_core.commissioning import acquire_commission_document
from quantbot.paper_core.contracts import CoreConfig
from quantbot.paper_core.release import RuntimeConfig, prepare_release
from quantbot.paper_core.store import CoreStore, table
from quantbot.storage import Database, StateConflictError


def commissioned(
    tmp_path, *, commit=True, isolated_repository=False, gross=False, learning_origin=None
):
    from quantbot.domain import Account
    from quantbot.paper_core.simulation import SyntheticBroker

    source = tmp_path / "canonical.db"
    Database.create(source).close()
    db = Database.create(tmp_path / "paper.db")
    repository = Path(__file__).resolve().parents[3]
    if isolated_repository:
        import shutil

        target = tmp_path / "release-source"
        for name in ("src", "deploy/omen"):
            shutil.copytree(
                repository / name, target / name, ignore=shutil.ignore_patterns("__pycache__")
            )
        for name in ("uv.lock", "pyproject.toml"):
            shutil.copyfile(repository / name, target / name)
        repository = target
    config = RuntimeConfig(
        repository=str(repository),
        store_path=str(db.path),
        store_identity=db.store_identity,
        expected_account_hash=hashlib.sha256(b"paper").hexdigest(),
        core=CoreConfig(
            account_id="paper",
            symbol="XLE",
            evidence_lane="BROKER_PAPER",
            release_id="UNBOUND_PREPARED_RELEASE",
            clock_profile="PAPER_TIME_V2",
            **(
                {"selection_policy": "SIGNAL_OR_EXPLORE_V1", "symbols": ("AAPL", "MSFT")}
                if gross
                else {}
            ),
        ),
        holdout_source=str(source),
        canonical_datasets=("us-equities-daily",),
        learning_origin=learning_origin or datetime.now(UTC) - timedelta(days=1),
        activation_policy="DAILY_BOUNDED_V3" if gross else "DAILY_BOUNDED_V2",
    )
    release = prepare_release(config)
    broker = SyntheticBroker(
        account_id="paper",
        now=datetime.now(UTC),
        initial_account=Account(
            account_id="paper", cash=100000, equity=100000, buying_power=100000, currency="USD"
        ),
    )
    broker.endpoint = "https://paper-api.alpaca.markets"
    document = asyncio.run(
        acquire_commission_document(
            release,
            broker,
            {
                "release_id": release.id,
                "approval_reference": "ISOLATED_MOCK_APPROVAL",
                "fenced_writer_evidence_ids": ["ISOLATED_NO_OTHER_WRITERS"],
                "legacy_owners": [],
            },
        )
    )
    store = CoreStore(db, derived_core(release, "paper"))
    commission = RuntimeCommission.model_validate(document["commission"])
    if commit:
        commission_runtime(store, release, commission, baseline=document["baseline"])
    return db, store, release, commission, document


def test_idle_closed_history_avoids_resolving_unchanged_lots(tmp_path, monkeypatch):
    from tests.fixtures.core_m5 import accepted

    db, store, broker, *_ = accepted(tmp_path)
    try:
        # Drain bounded migration/recovery work separately from healthy cadence.
        for _ in range(5):
            outcomes.process_outcomes(store, cutoff=broker.now)
        resolved = []
        original = outcomes._resolve

        def observe(*args, **kwargs):
            resolved.append(args[2])
            return original(*args, **kwargs)

        monkeypatch.setattr(outcomes, "_resolve", observe)
        batch = outcomes.process_outcomes(store, cutoff=broker.now)
        assert not batch.outcomes
        closed = {r["lot_id"] for r in store.documents("core_round_trips")}
        assert not closed.intersection(resolved), (
            "settled unchanged history must consume no per-lot resolver work"
        )
    finally:
        db.close()


def test_exact_commission_replay_preserves_only_original_collect(tmp_path):
    db, store, release, commission, document = commissioned(tmp_path)
    try:
        before = store.documents("core_runtime_events")
        assert len(store.documents("core_activations")) == 1
        assert (
            commission_runtime(store, release, commission, baseline=document["baseline"])
            == commission.id
        )
        initial = initialize_operational_model(
            store, release.config.initial_model, at=commission.approved_at
        )
        assert initial.at == commission.approved_at
        assert store.documents("core_runtime_events") == before
        assert (
            initialize_operational_model(
                store, release.config.initial_model, at=commission.approved_at
            ).id
            == initial.id
        )
        bad = commission.model_copy(update={"approval_reference": "DIFFERENT_APPROVAL"})
        with pytest.raises(StateConflictError, match="COMMISSION"):
            commission_runtime(store, release, bad, baseline=document["baseline"])
        with pytest.raises(StateConflictError, match="INITIAL_MODEL"):
            initialize_operational_model(
                store,
                release.config.initial_model,
                at=commission.approved_at + timedelta(seconds=1),
            )
        from quantbot.paper_core.activation import rollback_model

        rollback_model(
            store,
            expected_model_id=initial.model_id,
            target_id=initial.model_id,
            expected_activation_id=initial.id,
            reason="ISOLATED_EXPLICIT_FORWARD_EVENT",
            at=store.clocks.sample()[0],
        )
        with pytest.raises(StateConflictError, match="MODEL_ALREADY"):
            commission_runtime(store, release, commission, baseline=document["baseline"])
    finally:
        db.close()


def test_interrupted_initial_model_write_rolls_back_commission_then_exact_retry(
    tmp_path, monkeypatch
):
    from quantbot.paper_core import activation

    db, store, release, commission, document = commissioned(tmp_path, commit=False)
    try:
        original = activation._write_event

        def crash(*args, **kwargs):
            raise RuntimeError("INJECTED_INITIAL_ACTIVATION_INTERRUPTION")

        monkeypatch.setattr(activation, "_write_event", crash)
        with pytest.raises(RuntimeError, match="INTERRUPTION"):
            commission_runtime(store, release, commission, baseline=document["baseline"])
        for name in (
            "core_runtime_commissions",
            "core_fixture_baselines",
            "core_runtime_events",
            "core_models",
            "core_activations",
            "core_runtime_config_history",
        ):
            assert store.documents(name) == []
        monkeypatch.setattr(activation, "_write_event", original)
        config, path = store.config, db.path
        db.close()
        db = Database(path)
        store = CoreStore(db, config)
        commission_runtime(store, release, commission, baseline=document["baseline"])
        assert (
            datetime.fromisoformat(store.documents("core_activations")[0]["at"])
            == commission.approved_at
        )
    finally:
        db.close()


def test_supported_recovery_commands_are_explicit():
    from quantbot.paper_core.__main__ import parser

    for command in ("transition-release", "resume"):
        assert (
            parser()
            .parse_args(["--config", str(Path.cwd() / "prepared.json"), "--command", command])
            .command
            == command
        )


def test_dirty_historical_authority_is_not_daily_learning_input(tmp_path):
    from tests.fixtures.core_m5 import accepted

    from quantbot.paper_core.contracts import LearningPolicy
    from quantbot.paper_core.learning import _select
    from quantbot.paper_core.work import dirty

    db, store, broker, parent, *_ = accepted(tmp_path)
    try:
        # Raw historical evidence begins without the new coverage projection.
        # Use ordinary bounded recovery before asserting healthy learning readiness.
        for _ in range(5):
            outcomes.process_outcomes(store, cutoff=broker.now)
        with db.transaction() as session:
            policy = LearningPolicy(parent_model=parent)
            before, _, _ = _select(session, policy, broker.now, execution_now=broker.now)
            assert before
            lot = before[0]["lot_id"]
            outcome = before[0]["outcome_id"]
            dirty(session, (lot,))
            after, excluded, _ = _select(session, policy, broker.now, execution_now=broker.now)
            assert outcome not in {r["outcome_id"] for r in after}
            assert (outcome, "OUTCOME_RECONCILIATION_PENDING") in excluded
    finally:
        db.close()


def test_precommission_verifier_needs_no_store_env_provider_or_role(tmp_path):
    import subprocess
    import sys

    db, _, release, _, _ = commissioned(tmp_path)
    db.close()
    config = release.config.model_copy(
        update={
            "store_path": str(tmp_path / "never-opened.db"),
            "environment_file_reference": str(tmp_path / "never-read.env"),
        }
    )
    verifier = prepare_release(config)
    config_path, manifest_path = tmp_path / "config.json", tmp_path / "manifest.json"
    config_path.write_text(config.model_dump_json())
    manifest_path.write_text(verifier.model_dump_json())
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "quantbot.paper_core",
            "--config",
            str(config_path),
            "--manifest",
            str(manifest_path),
            "--command",
            "verify",
        ],
        text=True,
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["state"] == "PREPARED" and document["broker_state"] == "NOT_PROBED"
    assert (
        not Path(config.store_path).exists()
        and not Path(config.environment_file_reference).exists()
    )
    launcher = (Path(config.repository) / "deploy/omen/start-paper-core.ps1").read_text()
    assert "ParameterSetName='Verify'" in launcher and "@('--command','verify')" in launcher


def test_populated_v35_migration_preserves_all_prior_rows_and_typed_ids(tmp_path):
    from alembic import command
    from sqlalchemy import create_engine

    from quantbot.paper_core.contracts import Decision, ModelArtifact, ProtectionPlan, RoundTrip
    from quantbot.storage.schema import SCHEMA_VERSION

    fixture = json.loads(
        gzip.decompress(
            (Path(__file__).parents[2] / "fixtures/core_v35_accepted.json.gz").read_bytes()
        )
    )
    path = tmp_path / "accepted.db"
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        command.upgrade(Database._alembic_config(connection), "0035")
        connection.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
        for name, rows in fixture.items():
            if name in {"schema_version", "alembic_version"} or not rows:
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
    with Database.migrate(path) as db, db.engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT version FROM schema_version").scalar_one()
            == SCHEMA_VERSION
        )
        for name, prior in fixture.items():
            if name in {"schema_version", "alembic_version"}:
                continue
            rows = [
                dict(r) for r in connection.exec_driver_sql(f'SELECT * FROM "{name}"').mappings()
            ]
            assert sorted(rows, key=lambda r: json.dumps(r, sort_keys=True)) == sorted(
                prior, key=lambda r: json.dumps(r, sort_keys=True)
            )
        for name, kind in (
            ("core_decisions", Decision),
            ("core_models", ModelArtifact),
            ("core_protection_plans", ProtectionPlan),
            ("core_outcome_revisions", RoundTrip),
        ):
            for row in fixture[name]:
                assert kind.model_validate_json(row["payload"]).id == row["id"]
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"


def test_code_only_successor_preserves_economics_and_rejects_old_send_authority(tmp_path):
    from quantbot.paper_core.__main__ import commissioned_core
    from quantbot.paper_core.activation import active_in_session
    from quantbot.paper_core.authority import (
        executable_in_session,
        fence_entries,
        runtime_objections,
        transition_release,
    )
    from quantbot.paper_core.release import read_status

    db, store, old, commission, _ = commissioned(tmp_path, isolated_repository=True)
    try:
        before = {
            n: store.documents(n)
            for n in (
                "core_identities",
                "core_fixture_baselines",
                "core_activations",
                "core_models",
                "core_runtime_commissions",
            )
        }
        fence = fence_entries(
            store, reason="APPROVED_CODE_ONLY_MAINTENANCE", evidence_ids=("OLD_WRITER_FENCE",)
        )
        with db.transaction() as session:
            binding = executable_in_session(session)
            activation_id = active_in_session(store, session)[1].id
        source = Path(old.config.repository) / "src/quantbot/paper_core/work.py"
        source.write_bytes(source.read_bytes() + b"\n# Isolated code-only successor fixture.\n")
        successor = prepare_release(old.config)
        approval = dict(
            expected_binding_id=binding.id,
            old_release_id=old.id,
            new_release_id=successor.id,
            commission_id=commission.id,
            expected_fence_id=fence,
            expected_activation_id=activation_id,
            approval_reference="EXACT_OLD_TO_NEW_REVIEW",
            old_writer_fence_evidence_ids=["OLD_WRITER_FENCE"],
            approved_at=store.clocks.sample()[0].isoformat(),
        )
        incompatible = successor.model_copy(
            update={"config": successor.config.model_copy(update={"entry_poll_seconds": 14.0})}
        )
        with pytest.raises(StateConflictError, match="CODE_ONLY"):
            transition_release(store, incompatible, {**approval, "new_release_id": incompatible.id})
        transition_id = transition_release(store, successor, approval)
        assert transition_id != binding.id
        with db.transaction() as session:
            assert runtime_objections(store, session, entry=False) == (
                "CORE_RUNTIME_BINDING_MISMATCH",
            )
        current = CoreStore(db, store.config, runtime_release_id=successor.id)
        with db.transaction() as session:
            assert runtime_objections(current, session, entry=False) == ()
            assert runtime_objections(current, session, entry=True) == ("CORE_ENTRY_FENCED",)
            assert active_in_session(current, session)[1].id == activation_id
        assert commissioned_core(successor, read_status(db.path)) == store.config
        with pytest.raises(ValueError, match="BINDING"):
            commissioned_core(old, read_status(db.path))
        with pytest.raises(StateConflictError, match="APPROVAL"):
            transition_release(current, successor, approval)
        assert {n: store.documents(n) for n in before} == before
        print(
            "FINAL_EXECUTABLE_TRANSITION:"
            + json.dumps(
                {
                    "old_release": old.id,
                    "new_release": successor.id,
                    "economic_config": store.config_id,
                    "commission": commission.id,
                    "binding": transition_id,
                    "fence": fence,
                    "activation": activation_id,
                }
            )
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_dirty_closed_cost_restart_revocation_and_notification_during_processing(
    tmp_path, monkeypatch
):
    from tests.fixtures.core_m2 import NOW, fee_events, prepare

    from quantbot.paper_core.contracts import RoundTrip
    from quantbot.paper_core.guardian import acknowledge_handoffs, guardian_cycle, synchronize
    from quantbot.paper_core.learning import _metadata
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.paper_core.work import dirty, revoked_proof

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        decision = await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.now = NOW + timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        outcomes.import_broker_events(store, fee_events(store, broker))
        await synchronize(store, broker, now=broker.now)
        outcomes.process_outcomes(store, cutoff=broker.now)
        original_trip = RoundTrip.model_validate(store.documents("core_round_trips")[0])
        assert original_trip.status == "COMPLETE"
        # Passive history grows without changing the completed lot's economics.
        for _ in range(25):
            broker.now += timedelta(seconds=1)
            acknowledge_handoffs(store, now=broker.now)
            await synchronize(store, broker, now=broker.now)
        statements = []

        def sql_probe(conn, cursor, statement, parameters, context, many):
            statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", sql_probe)
        try:
            assert not outcomes.process_outcomes(store, cutoff=broker.now).outcomes
        finally:
            event.remove(db.engine, "before_cursor_execute", sql_probe)
        assert not any("BEGIN IMMEDIATE" in s for s in statements)
        assert not any(
            "core_broker_event_sources" in s or "core_guardian_receipts" in s for s in statements
        )
        # Obtain exact per-fill fee predecessors rather than rely on ID ordering.
        predecessors = {
            r["fill_id"]: __import__(
                "quantbot.research.manifest", fromlist=["content_id"]
            ).content_id(r)
            for r in store.documents("core_cost_items")
        }
        outcomes.import_broker_events(
            store, fee_events(store, broker, amount="0.10", revision=2, supersedes=predecessors)
        )
        broker.cash -= __import__("decimal").Decimal("0.20")
        await synchronize(store, broker, now=broker.now)
        config, path = store.config, db.path
        db.close()
        db = Database(path)
        store = CoreStore(db, config)
        resolved = outcomes.process_outcomes(store, cutoff=broker.now)
        assert len(resolved.outcomes) == 1 and resolved.outcomes[0].actual_fees == __import__(
            "decimal"
        ).Decimal("0.20")
        assert resolved.outcomes[0].supersedes == original_trip.id
        original_resolve = outcomes._resolve

        def concurrent_notification(*args, **kwargs):
            trip = original_resolve(*args, **kwargs)
            dirty(args[1], (args[2],))
            return trip

        with db.transaction() as session:
            dirty(session, (decision.id,))
        monkeypatch.setattr(outcomes, "_resolve", concurrent_notification)
        outcomes.process_outcomes(store, cutoff=broker.now)
        with db.transaction() as session:
            row = (
                session.execute(
                    select(table("core_outcome_work")).where(
                        table("core_outcome_work").c.lot_id == decision.id
                    )
                )
                .mappings()
                .one()
            )
            assert row["version"] > row["processed_version"]
        monkeypatch.setattr(outcomes, "_resolve", original_resolve)
        outcomes.process_outcomes(store, cutoff=broker.now)
        with db.transaction() as session:
            proof_id = resolved.outcomes[0].reconciliation_id
            certificates = table("core_quantity_certifications")
            session.execute(certificates.delete())
            for snapshot in session.execute(
                select(table("core_inventory_certifications").c.snapshot_id)
            ).scalars():
                revocations = table("core_cash_authority_revocations")
                if not session.execute(
                    select(revocations.c.snapshot_id).where(revocations.c.snapshot_id == snapshot)
                ).first():
                    session.execute(
                        revocations.insert().values(
                            snapshot_id=snapshot, reason="ISOLATED_PROOF_REVOCATION_FAULT"
                        )
                    )
            revoked_proof(session, proof_id)
        batch = outcomes.process_outcomes(store, cutoff=broker.now)
        assert not batch.outcomes and batch.reasons
        with db.transaction() as session:
            assert _metadata(session)[0]["status"] == "AUTHORITY_PENDING"
        await synchronize(store, broker, now=broker.now)
        outcomes.process_outcomes(store, cutoff=broker.now)
        with db.transaction() as session:
            assert _metadata(session)[0]["status"] == "COMPLETE"
        # Replay the same immutable source with a contradictory amount.
        source_payload = next(
            r
            for r in store.documents("core_broker_event_sources")
            if r.get("cost") and r["cost"]["revision"] == 2
        )
        from quantbot.paper_core.contracts import BrokerEvent

        source_event = BrokerEvent.model_validate(source_payload)
        contradictory = source_event.model_copy(
            update={"cost": source_event.cost.model_copy(update={"amount": Decimal("0.30")})}
        )
        with pytest.raises(StateConflictError, match="conflicting"):
            outcomes.import_broker_events(store, (contradictory,))
        assert not outcomes.process_outcomes(store, cutoff=broker.now).outcomes
        with db.transaction() as session:
            assert _metadata(session)[0]["status"] == "INTEGRITY_FAILURE"
        print(
            "FINAL_RETAINED_HISTORY:"
            + json.dumps(
                {
                    "idle_queries": len(statements),
                    "idle_writer_locks": 0,
                    "passive_receipts_added": 25,
                    "old_lot": decision.id,
                    "original_outcome": original_trip.id,
                    "corrected_outcome": resolved.outcomes[0].id,
                    "lost_dirty": False,
                }
            )
        )
    finally:
        db.close()


class ManualClocks:
    process_id = "offline-trader"

    def __init__(self, wall):
        self.wall, self.mono = wall, 100.0

    def sample(self):
        return self.wall, self.mono

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


def paper_mock(store, release):
    """Exact production adapter type with an entirely in-memory provider fixture."""
    from tests.fixtures.core_m2 import events, fee_events

    from quantbot.domain import Account
    from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
    from quantbot.paper_core.simulation import SyntheticBroker
    from quantbot.paper_core.time_evidence import BrokerTime

    synthetic = SyntheticBroker(
        account_id="paper",
        now=store.clocks.sample()[0],
        initial_account=Account(
            account_id="paper", cash=100000, buying_power=100000, equity=100000, currency="USD"
        ),
    )
    broker = object.__new__(AlpacaCoreBroker)
    broker.adapter, broker.store, broker.config, broker.clocks, broker.account_id = (
        synthetic,
        store,
        release.config,
        store.clocks,
        "paper",
    )
    broker.fixture_open = store.clocks.sample()[0] - timedelta(hours=1)
    broker.fixture_close = store.clocks.sample()[0] + timedelta(hours=6)
    broker.mark = synthetic.mark
    broker.fixture_fee_events = {}
    synthetic._transport = None  # No transport or shared HTTP budget in this in-memory fixture.
    broker.pending_progress = None
    broker.inventory_diagnostic_trace = []
    broker.entry_source_objections = ()
    broker._discovery_state = broker._research_state = None

    async def get_clock():
        store.clocks.advance(0.01)
        synthetic.now = store.clocks.sample()[0]
        return BrokerTime(
            timestamp=synthetic.now,
            account_id="paper",
            provider="alpaca",
            session_id="offline-session",
            session_open=broker.fixture_open,
            session_close=broker.fixture_close,
            resolution_seconds=0.000001,
        )

    async def owned_events(current):
        synthetic.now = current.clocks.sample()[0]
        for supplied in fee_events(current, synthetic, effective_start=broker.fixture_open):
            broker.fixture_fee_events.setdefault(supplied.cost.fill_id, supplied)
        return tuple(
            e.model_copy(
                update={"release_id": current.config.release_id, "evidence_lane": "BROKER_PAPER"}
            )
            for e in (*events(current, synthetic), *broker.fixture_fee_events.values())
        )

    broker.get_clock, broker.owned_events = get_clock, owned_events
    broker.accept_activity_cursor = lambda _: None
    return broker, synthetic


async def guardian_ready(store, broker):
    from quantbot.paper_core.authority import RoleHeartbeat, publish_heartbeat
    from quantbot.paper_core.guardian import acknowledge_handoffs, synchronize
    from quantbot.paper_core.time_evidence import acquire_time_evidence

    store.runtime_role = "guardian"
    store.clocks.process_id = "offline-independent-guardian"
    time_proof = await acquire_time_evidence(broker, store.clocks)
    store.record("core_time_evidence", time_proof)
    await synchronize(store, broker, now=store.clocks.sample()[0])
    receipt = acknowledge_handoffs(store, now=store.clocks.sample()[0])
    now = store.clocks.sample()[0]
    heartbeat = RoleHeartbeat(
        role="guardian",
        release_id=store.runtime_release_id,
        config_id=store.config_id,
        store_identity=store.database.store_identity,
        owner=store.config.owner,
        generation=store.config.generation,
        process_epoch=store.clocks.process_id,
        pid=os.getpid() + 100000,
        recorded_at=now,
        valid_until=now + timedelta(seconds=30),
        last_useful_progress=now,
        objections=(),
        evidence_ids=(time_proof.id,),
        proof_age_seconds=0,
        guardian_receipt_id=receipt.id,
    )
    publish_heartbeat(store, heartbeat)
    store.runtime_role = "trader"
    store.clocks.process_id = "offline-trader"
    return receipt, heartbeat, time_proof


@pytest.mark.asyncio
async def test_new_guardian_publication_does_not_replace_attested_current_receipt(tmp_path):
    from quantbot.paper_core.authority import guardian_objections, publish_heartbeat
    from quantbot.paper_core.contracts import GuardianReceipt
    from quantbot.paper_core.work import current_receipts

    db, store, release, _, _ = await asyncio.to_thread(commissioned, tmp_path)
    try:
        store.clocks = ManualClocks(store.clocks.sample()[0])
        broker, _ = paper_mock(store, release)
        old, heartbeat, _ = await guardian_ready(store, broker)
        store.clocks.advance(0.01)
        now = store.clocks.sample()[0]
        new = GuardianReceipt(
            recorded_at_utc=now,
            evidence_lane="BROKER_PAPER",
            release_id=store.config.release_id,
            owner=store.config.owner,
            generation=store.config.generation,
            valid_until=now + timedelta(seconds=120),
            protection_ids=("unattested-plan",),
        )
        store.record("core_guardian_receipts", new)
        with db.transaction() as session:
            assert current_receipts(session)[0].id == old.id
            assert current_receipts(session, plan_id="unattested-plan") == []
            assert guardian_objections(store, session, now=now, receipt_id=old.id) == ()
        next_heartbeat = heartbeat.model_copy(
            update={
                "recorded_at": now,
                "guardian_receipt_id": new.id,
                "valid_until": now + timedelta(seconds=30),
            }
        )
        publish_heartbeat(store, next_heartbeat)
        with db.transaction() as session:
            assert current_receipts(session, plan_id="unattested-plan")[0].id == new.id
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("independent", [False, True])
async def test_cash_fingerprint_exempts_only_independently_authoritative_allocation(
    tmp_path, independent
):
    from tests.fixtures.core_m2 import NOW, fee_events, prepare

    from quantbot.paper_core.contracts import BrokerEvent, CashActivity, ProtectionPlan
    from quantbot.paper_core.guardian import guardian_cycle, synchronize
    from quantbot.paper_core.send_guard import compatible_successor
    from quantbot.paper_core.trader import (
        claim_entry,
        evaluate_entry,
        record_observation,
        trader_cycle,
    )
    from quantbot.research.manifest import content_id

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.now += timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        if independent:
            activity = CashActivity(
                recorded_at_utc=broker.now,
                account_id=store.config.account_id,
                document_id="exact-independent-cash-control",
                activity_ids=("buy", "sell"),
                complete=True,
                watermark=broker.now,
                movements=tuple(
                    (f.fill_id, (-1 if f.side.value == "BUY" else 1) * f.quantity * f.price)
                    for f in broker.fills
                ),
            )
            event = BrokerEvent(
                recorded_at_utc=broker.now,
                occurred_at=broker.now,
                account_id=store.config.account_id,
                endpoint="synthetic/cash-control",
                event_id=activity.id,
                raw_digest=content_id(activity),
                client_order_id=broker.posts[0].client_order_id,
                cash_activity=activity,
            )
            outcomes.import_broker_events(store, (event,))
        zeros = fee_events(store, broker)
        outcomes.import_broker_events(store, zeros)
        await synchronize(store, broker, now=broker.now)
        original = store.latest_proof()
        saved = store.read("core_reconciliation_proofs", original.id, type(original)).model_dump(
            mode="json"
        )
        fresh = observation.model_copy(
            update={
                "source_start": broker.now - timedelta(minutes=5),
                "source_end": broker.now,
                "recorded_at_utc": broker.now,
                "available_at": broker.now,
                "dependency_available_at": (broker.now,),
                "sequence": 2,
                "raw_data_digest": "cash-fingerprint-control",
            }
        )
        decision = evaluate_entry(store.config, fresh, model, original)
        record_observation(store, fresh)
        store.record("core_decisions", decision)
        before = original.ledger_state_id
        broker.now += timedelta(seconds=1)
        outcomes.import_broker_events(
            store,
            fee_events(
                store,
                broker,
                amount="0.10",
                revision=2,
                supersedes={e.cost.fill_id: e.cost.id for e in zeros},
            ),
        )
        with db.transaction() as session:
            after = store.ledger_state_id(session)
            assert (before == after) is independent
            assert (
                compatible_successor(store, session, decision, original, None, now=broker.now)
                is independent
            )
        assert (
            store.read("core_reconciliation_proofs", original.id, type(original)).model_dump(
                mode="json"
            )
            == saved
        )
        if not independent:
            assert (
                claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
                is None
            )
            with pytest.raises(StateConflictError, match="REVOKED"):
                store.latest_proof()
            broker.cash -= Decimal("0.20")
            await synchronize(store, broker, now=broker.now)
            renewed = store.latest_proof()
            assert renewed.id != original.id and renewed.expected_cash == broker.cash
        else:
            assert store.latest_proof().id == original.id
            # Source cash ambiguity revokes independent acceptance; no cost exemption survives it.
            source = next(
                r for r in store.documents("core_broker_event_sources") if r.get("cash_activity")
            )
            disputed = content_id(
                {
                    "account": source["account_id"],
                    "provider": source["provider"],
                    "endpoint": source["endpoint"],
                    "event": source["event_id"],
                }
            )
            from quantbot.storage import StorageRepository

            with db.transaction() as session:
                StorageRepository(session).create_incident(
                    "cash-authority-fault",
                    severity="ERROR",
                    kind="CORE_EVIDENCE_CONFLICT",
                    message="ISOLATED_EXACT_CASH_SOURCE_DISPUTE",
                    occurred_at=broker.now,
                    detail={"implicated_source_ids": [disputed], "lot_ids": [decision.id]},
                )
                assert store.ledger_state_id(session) != original.ledger_state_id
                assert not compatible_successor(
                    store, session, decision, original, None, now=broker.now
                )
        print(
            "FINAL_CASH_FINGERPRINT:"
            + json.dumps(
                {
                    "independent": independent,
                    "original_proof": original.id,
                    "before": before,
                    "after_cost_allocation": after,
                    "old_payload_unchanged": True,
                }
            )
        )
    finally:
        db.close()


@pytest.mark.parametrize("change", ["fresh", "expired", "fenced"])
def test_original_candidate_retries_fresh_heartbeat_without_extending_authority(tmp_path, change):
    from tests.fixtures.core_m2 import NOW, prepare

    from quantbot.paper_core.authority import RoleHeartbeat, publish_heartbeat
    from quantbot.paper_core.guardian import acknowledge_handoffs
    from quantbot.paper_core.roles import retry_entry_candidate
    from quantbot.paper_core.trader import evaluate_entry, record_observation
    from quantbot.storage import StorageRepository

    db, store, _, proof, observation, model = prepare(tmp_path)
    try:
        store.clocks = ManualClocks(NOW)
        store.runtime_role = "guardian"
        receipt = acknowledge_handoffs(store, now=NOW)
        store.runtime_role = "trader"
        heartbeat = RoleHeartbeat(
            role="guardian",
            release_id=store.runtime_release_id,
            config_id=store.config_id,
            store_identity=db.store_identity,
            owner=store.config.owner,
            generation=store.config.generation,
            process_epoch="independent-short-heartbeat",
            proof_age_seconds=0,
            pid=os.getpid() + 100000,
            recorded_at=NOW - timedelta(seconds=1),
            valid_until=NOW,
            last_useful_progress=NOW - timedelta(seconds=1),
            objections=(),
            evidence_ids=(),
            guardian_receipt_id=receipt.id,
        )
        publish_heartbeat(store, heartbeat)
        record_observation(store, observation)
        original = evaluate_entry(store.config, observation, model, proof)
        assert original.action == "BUY"
        store.record("core_decisions", original)
        payload = original.model_dump_json()
        assert retry_entry_candidate(store, original) is original
        assert not store.links()
        publish_heartbeat(
            store,
            heartbeat.model_copy(
                update={
                    "recorded_at": NOW,
                    "valid_until": NOW + timedelta(seconds=30),
                    "last_useful_progress": NOW,
                }
            ),
        )
        if change == "expired":
            store.clocks.advance((original.valid_until - NOW).total_seconds())
        elif change == "fenced":
            store.runtime_entry_enabled = False
        retained = retry_entry_candidate(store, original)
        if change == "fresh":
            assert retained is None and len(store.links()) == 1
            intent_id = store.links()[0]["intent_id"]
            assert retry_entry_candidate(store, original) is None
            assert len(store.links()) == 1
            with db.transaction() as session:
                assert StorageRepository(session).get_order_intent(intent_id).client_order_id
        else:
            assert not store.links()
            assert retained is (original if change == "fenced" else None)
        assert original.model_dump_json() == payload
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["once", "repeated", "unrelated", "budget"])
async def test_trader_exact_certification_cas_retry_has_one_shared_budget(
    tmp_path, monkeypatch, failure
):
    from tests.fixtures.core_m6 import broker as fixture_broker
    from tests.integration.paper_core.test_release_recovery import make_process_release

    from quantbot.paper_core import guardian, trader

    db, store, config, _, *_ = make_process_release(tmp_path)
    broker = fixture_broker(store, config)
    store.runtime_role = "trader"
    original = guardian.synchronize
    calls = []
    budget = []
    original_timeout = asyncio.timeout

    def short_budget(seconds):
        budget.append(seconds)
        return original_timeout(0.2 if failure == "budget" else seconds)

    async def acquisition(*args, **kwargs):
        calls.append(store.clocks.sample()[1])
        if failure == "unrelated":
            raise TimeoutError("INVENTORY_ACQUISITION_UNSTABLE")
        if failure == "budget":
            await asyncio.sleep(0.125)
        if failure in {"repeated", "budget"} or len(calls) == 1:
            raise TimeoutError("RECONCILIATION_AUTHORITY_CHANGED")
        return await original(*args, **kwargs)

    monkeypatch.setattr(guardian, "synchronize", acquisition)
    monkeypatch.setattr(
        trader,
        "asyncio",
        type("Budget", (), {"timeout": staticmethod(short_budget)}),
        raising=False,
    )
    try:
        observation = await broker.observation()
        before = store.latest_proof().id
        decision = await trader.trader_cycle(
            store,
            broker,
            observation,
            config.initial_model,
            store.latest_proof(),
            now=store.clocks.sample()[0],
        )
        assert len(calls) == (1 if failure == "unrelated" else 2)
        assert budget == [5]
        assert decision.observation_id == observation.id
        assert decision.valid_until == observation.source_end + timedelta(seconds=120)
        if failure == "once":
            assert decision.action == "BUY" and store.latest_proof().id != before
            # This acquisition control supplies no independent role readiness.
            assert not store.links()
        else:
            assert decision.action == "WAIT"
            assert "INVENTORY_ACQUISITION_RETRYABLE" in decision.gates
            assert not store.links()
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger_changed", [False, True])
async def test_acquired_proof_generation_and_certification_share_writer_lock(
    tmp_path, monkeypatch, ledger_changed
):
    import sqlite3

    from tests.fixtures.core_m6 import broker as fixture_broker
    from tests.integration.paper_core.test_release_recovery import make_process_release

    from quantbot.paper_core.guardian import synchronize
    from quantbot.paper_core.store import latest_certification, put
    from quantbot.research.manifest import content_id

    db, store, config, _, *_ = make_process_release(tmp_path)
    broker = fixture_broker(store, config)
    before = store.documents("core_reconciliation_proofs")
    initial_generation = store.latest_proof().generation
    original_proof, original_account = store.proof, broker.get_account
    provider_reads = []

    def writer_available():
        probe = sqlite3.connect(db.path, timeout=0)
        try:
            probe.execute("BEGIN IMMEDIATE")
            return True
        except sqlite3.OperationalError as error:
            assert "locked" in str(error)
            return False
        finally:
            probe.rollback()
            probe.close()

    async def account():
        assert writer_available()  # Every provider read stays outside the writer lock.
        provider_reads.append(True)
        acquired = await original_account()
        if ledger_changed and len(provider_reads) == 2:
            external = dict(
                account_id=store.config.account_id,
                symbol="XLE",
                quantity_delta="0",
                net_cash="1",
                order_id="offline-external-cash",
                occurred_at=store.clocks.sample()[0].isoformat(),
                activity_type="CASH",
                document_id="offline-external-source",
            )
            with db.transaction() as session:
                put(session, "core_external_owner_events", content_id(external), external)
        return acquired

    def proof(*args, **kwargs):
        assert not writer_available()  # Competing certification cannot change the generation gap.
        return original_proof(*args, **kwargs)

    monkeypatch.setattr(broker, "get_account", account)
    monkeypatch.setattr(store, "proof", proof)
    try:
        if ledger_changed:
            with pytest.raises(StateConflictError, match="INVENTORY_MISMATCH"):
                await synchronize(store, broker, now=store.clocks.sample()[0])
            with db.transaction() as session:
                assert latest_certification(session)[0] == initial_generation
            with pytest.raises(StateConflictError, match="CASH_AUTHORITY_STALE"):
                store.latest_proof()
        else:
            await synchronize(store, broker, now=store.clocks.sample()[0])
            assert store.latest_proof().generation == initial_generation + 1
            await synchronize(store, broker, now=store.clocks.sample()[0])
            assert store.latest_proof().generation == initial_generation + 2
        assert all(doc in store.documents("core_reconciliation_proofs") for doc in before)
        assert provider_reads
    finally:
        db.close()


@pytest.mark.asyncio
async def test_proof_construction_refusal_keeps_original_error_and_no_authority(
    tmp_path, monkeypatch
):
    from tests.fixtures.core_m6 import broker as fixture_broker
    from tests.integration.paper_core.test_release_recovery import make_process_release

    from quantbot.paper_core.guardian import synchronize

    db, store, config, _, *_ = make_process_release(tmp_path)
    original_error = StateConflictError("EXACT_PROOF_CONSTRUCTION_REFUSED")
    before = store.documents("core_reconciliation_proofs")
    initial = store.latest_proof().id
    with db.transaction() as session:
        authority = {
            name: [dict(row) for row in session.execute(select(table(name))).mappings()]
            for name in (
                "core_inventory_certifications",
                "core_quantity_certifications",
                "core_outcome_work",
            )
        }

    def refuse(*args, **kwargs):
        raise original_error

    monkeypatch.setattr(store, "proof", refuse)
    try:
        with pytest.raises(StateConflictError, match="EXACT_PROOF_CONSTRUCTION_REFUSED") as caught:
            await synchronize(store, fixture_broker(store, config), now=store.clocks.sample()[0])
        assert caught.value is original_error
        assert store.documents("core_reconciliation_proofs") == before
        assert store.latest_proof().id == initial
        with db.transaction() as session:
            assert authority == {
                name: [dict(row) for row in session.execute(select(table(name))).mappings()]
                for name in authority
            }
        assert not store.links()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_fence_owned_exit_bad_resume_fresh_resume_new_claim_and_stale_send(tmp_path):
    from quantbot.config import Settings
    from quantbot.paper_core.activation import active_in_session
    from quantbot.paper_core.authority import (
        executable_in_session,
        fence_entries,
        resume_entries,
        transition_release,
    )
    from quantbot.paper_core.contracts import Observation
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.roles import dispatch_entries
    from quantbot.paper_core.send_guard import final_send_objections
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.risk import OrderPurpose
    from quantbot.storage import StorageRepository

    db, store, release, commission, _ = await asyncio.to_thread(
        commissioned, tmp_path, isolated_repository=True
    )
    try:
        store.clocks = ManualClocks(store.clocks.sample()[0])
        store.runtime_settings = Settings(
            _env_file=None,
            EXPECTED_ACCOUNT_ID="paper",
            ALPACA_PAPER_API_KEY="offline-key",
            ALPACA_PAPER_API_SECRET="offline-secret",
            KILL_SWITCH=False,
            BROKER_HEALTHY=True,
            MARKET_DATA_HEALTHY=True,
            RISK_ENGINE_HEALTHY=True,
            RECONCILIATION_SUCCESSFUL=True,
        )
        store.runtime_submission_enabled = True
        with db.transaction() as session:
            StorageRepository(session).set_kill_switch(
                False, reason="EXPLICIT_ISOLATED_FIXTURE", updated_at=store.clocks.sample()[0]
            )
        broker, synthetic = paper_mock(store, release)
        await guardian_ready(store, broker)
        now = store.clocks.sample()[0]
        observation = Observation(
            recorded_at_utc=now,
            release_id=store.config.release_id,
            evidence_lane="BROKER_PAPER",
            symbol="XLE",
            feed="alpaca",
            source_start=now - timedelta(minutes=35),
            source_end=now,
            available_at=now,
            price=100,
            features=(("return_5m", 0.01),),
            feature_version="five-minute-price-alpha-v1",
            raw_data_digest="offline-first-bar",
            dependencies=("SPY",),
            dependency_available_at=(now,),
            sequence=1,
            session_close=broker.fixture_close,
        )
        decision = await trader_cycle(
            store, broker, observation, release.config.initial_model, store.latest_proof(), now=now
        )
        assert decision.action == "BUY" and store.links(), decision.gates
        await guardian_ready(store, broker)
        from quantbot.brokers import SubmissionUncertainError
        from quantbot.domain import IntentState

        async def lost_ack(intent):
            await synthetic.submit_order(intent)
            raise SubmissionUncertainError("ISOLATED_LOST_ACK_AFTER_BROKER_EFFECT")

        async def unknown_lookup(*args, **kwargs):
            return None

        broker.submit_order, broker.get_order_by_client_id = lost_ack, unknown_lookup
        send_reasons = await dispatch_entries(store, broker)
        assert len(synthetic.posts) == 1, send_reasons
        original_plan = store.links()[0]["protection_id"]
        fence = fence_entries(
            store, reason="APPROVED_RECOVERY", evidence_ids=("EXPLICIT_ENTRY_WRITER_FENCE",)
        )
        with db.transaction() as session:
            old_binding = executable_in_session(session)
            activation_id = active_in_session(store, session)[1].id
            old_intent = StorageRepository(session).get_order_intent(store.links()[0]["intent_id"])
            assert old_intent.state is IntentState.SUBMISSION_UNKNOWN
        source = Path(release.config.repository) / "src/quantbot/paper_core/work.py"
        source.write_bytes(source.read_bytes() + b"\n# Offline approved code-only repair.\n")
        successor = prepare_release(release.config)
        transition_release(
            store,
            successor,
            dict(
                expected_binding_id=old_binding.id,
                old_release_id=release.id,
                new_release_id=successor.id,
                commission_id=commission.id,
                expected_fence_id=fence,
                expected_activation_id=activation_id,
                approved_at=store.clocks.sample()[0].isoformat(),
                approval_reference="OFFLINE_EXACT_EXECUTABLE_APPROVAL",
                old_writer_fence_evidence_ids=["EXPLICIT_ENTRY_WRITER_FENCE"],
            ),
        )
        assert "CORE_RUNTIME_BINDING_MISMATCH" in final_send_objections(
            store, old_intent, OrderPurpose.ENTRY, clock=lambda: store.clocks.sample()[0]
        )
        old_store = store
        store = CoreStore(
            db, old_store.config, clocks=old_store.clocks, runtime_release_id=successor.id
        )
        store.runtime_settings, store.runtime_submission_enabled = old_store.runtime_settings, True
        broker.store = store
        with db.transaction() as session:
            assert (
                StorageRepository(session).get_order_intent(old_intent.intent_id).client_order_id
                == old_intent.client_order_id
            )
            assert (
                StorageRepository(session).get_order_intent(old_intent.intent_id).state
                is IntentState.SUBMISSION_UNKNOWN
            )
        with pytest.raises(StateConflictError, match="UNRESOLVED_SUBMISSION"):
            resume_entries(
                store,
                dict(
                    commission_id=commission.id,
                    expected_fence_id=fence,
                    runtime_release_id=successor.id,
                    expected_activation_id=activation_id,
                    approved_at=store.clocks.sample()[0].isoformat(),
                    approval_reference="OFFLINE_RESUME_REFUSED_UNKNOWN",
                ),
            )
        assert len(synthetic.posts) == 1
        broker.submit_order, broker.get_order_by_client_id = (
            synthetic.submit_order,
            synthetic.get_order_by_client_id,
        )
        # Original plan survives handover; guardian completes its owned exit while fenced.
        store.clocks.advance(1801)
        store.runtime_role = "guardian"
        store.clocks.process_id = "offline-independent-guardian"
        exit_receipt = await guardian_cycle(store, broker, now=store.clocks.sample()[0])
        assert exit_receipt.exit_intent_ids and len(synthetic.posts) == 2
        assert store.links()[0]["protection_id"] == original_plan
        assert store.documents("core_round_trips")[0]["decision_id"] == decision.id
        receipt, heartbeat, time_proof = await guardian_ready(store, broker)
        request = dict(
            commission_id=commission.id,
            expected_fence_id=fence,
            runtime_release_id=successor.id,
            expected_activation_id=activation_id,
            guardian_receipt_id=receipt.id,
            guardian_heartbeat_id=heartbeat.id,
            proof_id=store.latest_proof().id,
            time_evidence_id=time_proof.id,
            approved_at=store.clocks.sample()[0].isoformat(),
            approval_reference="OFFLINE_EXPLICIT_RESUME_APPROVAL",
        )
        with pytest.raises(StateConflictError, match="CURRENT"):
            resume_entries(store, {**request, "expected_fence_id": "stale"})
        with db.transaction() as session:
            StorageRepository(session).set_kill_switch(
                True, reason="SEPARATE_ISOLATED_MANUAL_PAUSE", updated_at=store.clocks.sample()[0]
            )
        with pytest.raises(StateConflictError, match="MANUAL"):
            resume_entries(store, request)
        with db.transaction() as session:
            assert StorageRepository(session).get_kill_switch_state().engaged
            StorageRepository(session).set_kill_switch(
                False, reason="EXPLICIT_FIXTURE_STOP_RESUME", updated_at=store.clocks.sample()[0]
            )
        resume = resume_entries(store, request)
        with pytest.raises(StateConflictError, match="CURRENT"):
            resume_entries(store, request)
        now = store.clocks.sample()[0]
        fresh = observation.model_copy(
            update={
                "recorded_at_utc": now,
                "source_start": now - timedelta(minutes=35),
                "source_end": now,
                "available_at": now,
                "dependency_available_at": (now,),
                "sequence": 2,
                "raw_data_digest": "offline-second-bar",
            }
        )
        new_decision = await trader_cycle(
            store, broker, fresh, successor.config.initial_model, store.latest_proof(), now=now
        )
        assert (
            new_decision.action == "BUY"
            and len([r for r in store.links() if r["role"] == "ENTRY"]) == 2
        )
        new_fence = fence_entries(
            store, reason="OFFLINE_NEW_FENCE_AFTER_CLAIM", evidence_ids=(resume,)
        )
        with db.transaction() as session:
            current_intent = StorageRepository(session).get_order_intent(
                next(r for r in store.links() if r["decision_id"] == new_decision.id)["intent_id"]
            )
        assert "CORE_ENTRY_FENCED" in final_send_objections(
            store, current_intent, OrderPurpose.ENTRY, clock=lambda: store.clocks.sample()[0]
        )
        print(
            "FINAL_RECOVERY_CHAIN:"
            + json.dumps(
                {
                    "commission": commission.id,
                    "original_plan": original_plan,
                    "original_decision": decision.id,
                    "fence": fence,
                    "resume": resume,
                    "new_claim": new_decision.id,
                    "new_fence": new_fence,
                    "posts": len(synthetic.posts),
                    "mock_only": True,
                }
            )
        )
    finally:
        db.close()
