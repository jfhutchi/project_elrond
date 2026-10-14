"""Execution-time regressions, using immutable original M4 synthetic input replay."""

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.fixtures.core_m4 import ExecutionClock

from quantbot.paper_core.contracts import CoreConfig, LearningPolicy, ModelArtifact
from quantbot.paper_core.store import CoreStore, put, table
from quantbot.storage import Database
from quantbot.storage.schema import search_runs


def replay_inputs(tmp_path):
    """Scoped fault fixture: exact original source joins, not new execution evidence."""
    from quantbot.paper_core.contracts import Decision, Observation, RoundTrip

    root = Path(__file__).parents[3]
    source = json.loads(
        gzip.decompress((root / "docs/evidence/paper-core/m4/joined-update.json.gz").read_bytes())
    )
    db = Database.create(tmp_path / "timing.db")
    store = CoreStore(db, CoreConfig.model_validate(source["joins"][0]["config"]))
    with db.transaction() as session:
        for joined in source["joins"]:
            decision, observation, outcome = (
                kind.model_validate(joined[name])
                for kind, name in (
                    (Decision, "decision"),
                    (Observation, "observation"),
                    (RoundTrip, "round_trip"),
                )
            )
            for name, value in (
                ("core_decisions", decision),
                ("core_observations", observation),
                ("core_outcome_revisions", outcome),
            ):
                put(session, name, value.id, value)
            session.execute(
                table("core_outcome_index")
                .insert()
                .values(
                    lot_id=outcome.lot_id,
                    outcome_id=outcome.id,
                    available_at=outcome.available_at.isoformat(),
                    source_start=outcome.source_start.isoformat(),
                    source_end=outcome.source_end.isoformat(),
                    dataset=outcome.dataset,
                    policy_id=outcome.policy_id,
                    evidence_lane=outcome.evidence_lane,
                    status=outcome.status,
                )
            )
    cutoff = datetime.fromisoformat(source["receipt"]["cutoff"])
    return (
        db,
        store,
        LearningPolicy.legacy_fixture(parent_model=ModelArtifact.model_validate(source["parent"])),
        cutoff,
    )


def test_no_change_completion_is_execution_time_and_replay_is_immutable(tmp_path):
    from quantbot.paper_core.learning import consider_learning

    with Database.create(tmp_path / "empty.db") as db:
        clock = ExecutionClock(datetime(2026, 10, 14, 17, tzinfo=UTC))
        store = CoreStore(
            db, CoreConfig(account_id="synthetic-account", symbol="XLE"), clocks=clock
        )
        cutoff = clock.at - timedelta(days=100)
        receipt = consider_learning(
            store, LearningPolicy.legacy_fixture(), cutoff=cutoff, legacy_fixture_replay=True
        )
        assert receipt.recorded_at_utc == clock.at, "completion must not be input cutoff"
        assert receipt.completed_at == receipt.available_at == clock.at
        assert receipt.started_at == clock.at and receipt.fit_started_at is None
        assert receipt.execution_clock_domain == clock.execution_domain
        clock.advance(120)
        assert (
            consider_learning(
                store, LearningPolicy.legacy_fixture(), cutoff=cutoff, legacy_fixture_replay=True
            )
            == receipt
        )


def test_delayed_fit_has_actual_charge_start_and_completion(tmp_path, monkeypatch):
    from quantbot.paper_core import learning

    db, store, policy, cutoff = replay_inputs(tmp_path)
    clock = ExecutionClock(cutoff + timedelta(days=2))
    store.clocks = clock
    start = clock.at
    original = learning._fit_subprocess

    def delayed(*args, **kwargs):
        clock.advance(12)
        return original(*args, **kwargs)

    monkeypatch.setattr(learning, "_fit_subprocess", delayed)
    try:
        receipt = learning.consider_learning(
            store, policy, cutoff=cutoff, legacy_fixture_replay=True
        )
        assert receipt.status == "AWAITING_ACTIVATION"
        assert receipt.recorded_at_utc == start + timedelta(seconds=12)
        assert receipt.started_at == receipt.fit_started_at == start
        assert receipt.completed_at == receipt.available_at == clock.at > cutoff
        with db.transaction() as session:
            row = session.execute(select(search_runs)).mappings().one()
            assert datetime.fromisoformat(row["started_at"]) == start
            assert row["started_at"] == row["finished_at"]
            detail = json.loads(row["detail_json"])
            assert detail["event"] == "PERMANENT_TRIAL_CHARGE"
            assert detail["fit_completed_at"] is None
        clock.advance(3600)
        assert (
            learning.consider_learning(store, policy, cutoff=cutoff, legacy_fixture_replay=True)
            == receipt
        )
        print("M4_FIX1_DELAYED:" + receipt.model_dump_json())
    finally:
        db.close()


@pytest.mark.parametrize("cross_domain", [False, True])
def test_later_recovery_has_later_availability_without_recharging(
    tmp_path, monkeypatch, cross_domain
):
    from quantbot.paper_core import learning
    from quantbot.research.registry import HypothesisRegistry

    db, store, policy, cutoff = replay_inputs(tmp_path)
    clock = ExecutionClock(cutoff + timedelta(days=2))
    store.clocks = clock
    start = clock.at

    def crash(*args, **kwargs):
        raise SystemExit(23)

    monkeypatch.setattr(learning, "_fit_subprocess", crash)
    try:
        with pytest.raises(SystemExit):
            learning.consider_learning(store, policy, cutoff=cutoff, legacy_fixture_replay=True)
        clock.advance(86400)
        if cross_domain:
            clock.execution_domain = "SYNTHETIC_TEST:separate-recovery-domain"
        recovered = learning.consider_learning(
            store, policy, cutoff=cutoff, legacy_fixture_replay=True
        )
        assert recovered.status == "FIT_FAILED" and recovered.reason == "PRIOR_PROCESS_LOST"
        assert recovered.recorded_at_utc == clock.at
        assert recovered.started_at == start
        assert recovered.completed_at == clock.at
        assert recovered.available_at == (None if cross_domain else clock.at)
        assert recovered.execution_clock_domain == ExecutionClock.execution_domain
        assert recovered.completion_clock_domain == clock.execution_domain
        with db.transaction() as session:
            assert HypothesisRegistry(session).learner_trials_spent() == 1
            row = session.execute(select(search_runs)).mappings().one()
            assert datetime.fromisoformat(row["finished_at"]) == start
        clock.advance(86400)
        assert (
            learning.consider_learning(store, policy, cutoff=cutoff, legacy_fixture_replay=True)
            == recovered
        )
        print("M4_FIX1_LATER_RECOVERY:" + recovered.model_dump_json())
    finally:
        db.close()


@pytest.mark.parametrize("role", ["PROTECTED_EVALUATION", "FORWARD_PAPER"])
def test_future_cutoff_does_not_expire_live_reservation_before_data(tmp_path, monkeypatch, role):
    from tests.unit.research.test_registry import make_draft, register, window

    from quantbot.paper_core import learning
    from quantbot.research.registry import DataRole, HypothesisRegistry

    db, store, policy, cutoff = replay_inputs(tmp_path)
    clock = ExecutionClock(cutoff + timedelta(days=2))
    store.clocks = clock
    try:
        with db.transaction() as session:
            register(
                HypothesisRegistry(session),
                make_draft(
                    windows=(
                        window(DataRole(role), "2026-10-14", "2026-10-27", dataset=policy.dataset),
                    )
                ),
                now=clock.at,
            )
        touched = []

        def forbidden(*args, **kwargs):
            touched.append(True)
            raise AssertionError("LIVE_RESERVATION_MATERIALIZED")

        monkeypatch.setattr(learning, "_materialize", forbidden)
        receipt = learning.consider_learning(
            store, policy, cutoff=cutoff + timedelta(days=365), legacy_fixture_replay=True
        )
        assert not touched, "future input cutoff must not age live reservations"
        assert receipt.status == "NO_CHANGE" and len(receipt.exclusions) == 40
        assert {reason for _, reason in receipt.exclusions} == {
            "CANONICAL_PROTECTED_OR_FORWARD_WINDOW"
        }
    finally:
        db.close()


def test_materialization_recheck_uses_fresh_execution_time(tmp_path, monkeypatch):
    from tests.unit.research.test_registry import make_draft, register, window

    from quantbot.paper_core import learning
    from quantbot.research.registry import DataRole, HypothesisRegistry

    db, store, policy, cutoff = replay_inputs(tmp_path)
    clock = ExecutionClock(cutoff + timedelta(days=2))
    store.clocks = clock
    original = learning._select

    def newly_reserved(session, *args, **kwargs):
        result = original(session, *args, **kwargs)
        clock.advance(86400)
        register(
            HypothesisRegistry(session),
            make_draft(
                windows=(
                    window(
                        DataRole.PROTECTED_EVALUATION,
                        "2026-10-14",
                        "2026-10-27",
                        dataset=policy.dataset,
                    ),
                )
            ),
            now=clock.at,
        )
        return result

    touched = []

    def forbidden(*args, **kwargs):
        touched.append(True)
        raise AssertionError("RECHECK_LIVE_RESERVATION_MATERIALIZED")

    monkeypatch.setattr(learning, "_select", newly_reserved)
    monkeypatch.setattr(learning, "_materialize", forbidden)
    try:
        receipt = learning.consider_learning(
            store, policy, cutoff=cutoff + timedelta(days=365), legacy_fixture_replay=True
        )
        assert not touched
        assert (
            receipt.status == "FIT_FAILED"
            and receipt.reason == "HOLDOUT_CHANGED_BEFORE_MATERIALIZATION"
        )
        assert receipt.completed_at == receipt.available_at == clock.at
    finally:
        db.close()


def test_populated_old_v33_receipts_and_artifacts_keep_ids_and_unknown_availability(tmp_path):
    from quantbot.paper_core.contracts import LearningReceipt
    from quantbot.research.manifest import content_id

    fixture = json.loads(
        (Path(__file__).parents[2] / "fixtures/core_v33_m4_learning.json").read_text()
    )
    with Database.create(tmp_path / "old-v33.db") as db:
        with db.transaction() as session:
            for name in (
                "core_identities",
                "core_models",
                "core_learning_runs",
                "core_learning_inputs",
                "search_runs",
                "core_learning_attempts",
                "core_learning_considerations",
            ):
                for row in fixture[name]:
                    session.execute(table(name).insert().values(**row))
        store = CoreStore(
            db, CoreConfig.model_validate_json(fixture["core_identities"][0]["payload"])
        )
        for name, kind in (("core_learning_runs", LearningReceipt), ("core_models", ModelArtifact)):
            for row in fixture[name]:
                old = store.read(name, row["id"], kind)
                assert old.id == row["id"] == content_id(json.loads(row["payload"]))
                if isinstance(old, LearningReceipt):
                    assert old.completed_at is None and old.available_at is None
                    assert "available_at" not in old.model_dump(mode="json")
                    store.record(name, old)
        with db.transaction() as session:
            for name in fixture:
                if name in {
                    "search_runs",
                    "core_learning_attempts",
                    "core_learning_considerations",
                }:
                    assert [
                        dict(r) for r in session.execute(select(table(name))).mappings()
                    ] == fixture[name]
                else:
                    assert dict(
                        session.execute(select(table(name).c.id, table(name).c.payload)).all()
                    ) == {r["id"]: r["payload"] for r in fixture[name]}


@pytest.mark.parametrize("status", ["FIT_FAILED", "CANDIDATE_REJECTED"])
def test_failed_and_rejected_fits_have_completion_availability(tmp_path, monkeypatch, status):
    from quantbot.paper_core import learning

    db, store, policy, cutoff = replay_inputs(tmp_path)
    clock = ExecutionClock(cutoff + timedelta(days=2))
    original = learning._fit_subprocess

    def end_work(*args, **kwargs):
        clock.advance(8)
        if status == "FIT_FAILED":
            raise TimeoutError("fixture child timeout")
        return original(*args, **kwargs)

    monkeypatch.setattr(learning, "_fit_subprocess", end_work)
    if status == "CANDIDATE_REJECTED":
        monkeypatch.setattr(learning, "_passes_validation", lambda *args: False)
    try:
        receipt = learning.consider_learning(
            store, policy, cutoff=cutoff, execution_clock=clock, legacy_fixture_replay=True
        )
        assert receipt.status == status
        assert receipt.completed_at == receipt.available_at == receipt.recorded_at_utc == clock.at
        assert (
            receipt.resulting_model_id == policy.parent_model.id and receipt.activation_at is None
        )
    finally:
        db.close()


def test_busy_refusal_has_actual_completion_availability(tmp_path):
    from quantbot.paper_core.learning import consider_learning
    from quantbot.storage.lock import SingleWriterLock

    with Database.create(tmp_path / "busy.db") as db:
        store = CoreStore(db, CoreConfig(account_id="synthetic-account", symbol="XLE"))
        clock = ExecutionClock(datetime(2026, 10, 14, 17, tzinfo=UTC))
        lock_path = db.path.parent / (db.path.name + ".learning-scratch") / "admission.lock"
        with SingleWriterLock(lock_path):
            receipt = consider_learning(
                store,
                LearningPolicy.legacy_fixture(),
                cutoff=clock.at - timedelta(days=10),
                execution_clock=clock,
            )
        assert receipt.reason == "LEARNING_WORKER_BUSY"
        assert receipt.recorded_at_utc == receipt.completed_at == receipt.available_at == clock.at
