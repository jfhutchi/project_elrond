"""Scheduled context research uses explicit synthetic stores and canonical admission."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from tests.fixtures.core_m2 import prepare
from tests.unit.learner.test_context_feedback import review
from tests.unit.learner.test_context_integration import news, stores
from tests.unit.paper_core.test_external_context import NOW, permissions

from quantbot.learner.context_admission import (
    CanonicalContextAdmission,
    ContextEvaluationPlan,
    ContextReadingScope,
    register_context_plan,
)
from quantbot.paper_core.context_integration import capture_research_context
from quantbot.paper_core.context_learning import (
    BINDING_KEY,
    PROGRESS_KEY,
    STATUS_KEY,
    ContextLearningBinding,
    consider_context_learning,
)
from quantbot.research.manifest import content_id
from quantbot.storage import Database


def fixture(tmp_path):
    research, experiments = stores(tmp_path)
    source_permissions = {"alpaca-news": permissions()}
    with research.connection() as db:
        research._set(db, "context:source-permissions", list(source_permissions.values()))
    registry = tmp_path / "canonical-synthetic.db"
    Database.create(registry).close()
    plan = ContextEvaluationPlan(
        experiment_id="synthetic-context",
        recorded_at=NOW - timedelta(days=1),
        manifest_hash=experiments.state("synthetic-context").manifest_hash,
        source_start=NOW - timedelta(days=1),
        training_start=NOW,
        split_at=NOW + timedelta(days=10),
        evaluation_end=NOW + timedelta(days=20),
        reading_scope=ContextReadingScope(source_policy_id=content_id(source_permissions)),
    )
    register_context_plan(experiments, plan, now=plan.recorded_at)
    core = tmp_path / "core"
    core.mkdir()
    db, store, _, _, _, _ = prepare(core)
    binding = ContextLearningBinding(
        experiment_id=plan.experiment_id,
        core_store_identity=store.database.store_identity,
        experiment_store_path=experiments.path,
        registry_path=registry,
        declaration_path=registry,
        manifest_hash=plan.manifest_hash,
        plan_id=plan.plan_id,
        recorded_at=plan.recorded_at,
        capture_rowid=0,
        decision_rowid=0,
        outcome_rowid=0,
    )
    with research.connection() as db:
        research._set(db, BINDING_KEY, binding)
    broker = SimpleNamespace(research_state=research)
    return db, store, broker, research, experiments, plan, registry


def test_missing_reviews_are_explicit_and_idle_poll_does_no_work(tmp_path, monkeypatch):
    db, store, broker, research, experiments, plan, registry = fixture(tmp_path)
    try:
        news(research)
        snapshot = capture_research_context(
            research, symbol="AAPL", at=NOW, permissions={"alpaca-news": permissions()}
        )
        assert snapshot is not None
        first = consider_context_learning(store, broker, now=NOW)
        assert first.snapshots_journaled == 1 and not first.candidate_id
        assert "INDEPENDENT_REVIEW_UNAVAILABLE" in dict(first.gate_counts)
        assert first.grants_activation is False
        with research.connection() as connection:
            before = research._get(connection, PROGRESS_KEY)
            saved = research._get(connection, STATUS_KEY)
        monkeypatch.setattr(
            "quantbot.paper_core.context_learning._core_batch",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("idle scan")),
        )
        assert consider_context_learning(store, broker, now=NOW + timedelta(seconds=15)) == first
        with research.connection() as connection:
            assert research._get(connection, PROGRESS_KEY) == before
            assert research._get(connection, STATUS_KEY) == saved
        assert (
            CanonicalContextAdmission(
                plan, registry_path=registry, declaration_path=registry, now=plan.evaluation_end
            ).used_calls(plan.experiment_id)
            == 0
        )
    finally:
        db.close()


def test_scheduler_progresses_with_independent_reviews_and_charges_existing_registry_once(tmp_path):
    db, store, broker, research, experiments, plan, registry = fixture(tmp_path)
    try:
        for index in range(16):
            news(research, index)
            at = NOW + timedelta(days=index)
            snapshot = capture_research_context(
                research,
                symbol="AAPL",
                at=at,
                permissions={"alpaca-news": permissions()},
                maximum_documents=1,
            )
            experiments.append(review(snapshot.readings[0]), idempotency_key=f"review:{index}")
            status = consider_context_learning(store, broker, now=at, force=True)
            assert status.state == "COLLECTING"
        result = consider_context_learning(store, broker, now=plan.evaluation_end, force=True)
        assert result.state == "EVALUATED" and result.candidate_id
        assert result.eligible_examples == 16 and result.grants_activation is False
        again = consider_context_learning(
            store, broker, now=plan.evaluation_end + timedelta(days=1), force=True
        )
        assert again == result
        admission = CanonicalContextAdmission(
            plan, registry_path=registry, declaration_path=registry, now=plan.evaluation_end
        )
        assert admission.used_calls(plan.experiment_id) == 1
        assert (
            len(
                experiments.read_as_of(
                    plan.experiment_id,
                    plan.evaluation_end + timedelta(days=2),
                    maximum_records=4000,
                    kinds=("ContextEvaluation",),
                )
            )
            == 1
        )
        news(research, 21)
        capture_research_context(
            research,
            symbol="AAPL",
            at=NOW + timedelta(days=21),
            permissions={"alpaca-news": permissions()},
            maximum_documents=1,
        )
        future = consider_context_learning(store, broker, now=NOW + timedelta(days=21), force=True)
        assert future.snapshots_journaled == 17
        assert future.candidate_id == result.candidate_id
        assert admission.used_calls(plan.experiment_id) == 1
    finally:
        db.close()


def test_uncommissioned_or_changed_source_scope_never_fits(tmp_path):
    db, store, broker, research, _, plan, registry = fixture(tmp_path)
    try:
        with research.connection() as connection:
            research._set(connection, "context:source-permissions", [])
        result = consider_context_learning(store, broker, now=plan.evaluation_end)
        assert "CONTEXT_READING_SCOPE_OR_SOURCE_POLICY_UNVERIFIED" in dict(result.gate_counts)
        assert not result.candidate_id
        assert (
            CanonicalContextAdmission(
                plan, registry_path=registry, declaration_path=registry, now=plan.evaluation_end
            ).used_calls(plan.experiment_id)
            == 0
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_existing_daily_job_considers_context_before_stock_session_early_exit(monkeypatch):
    from quantbot.paper_core.roles import daily_consideration

    calls = []

    async def calendar():
        return SimpleNamespace(sequence=SimpleNamespace(sessions=()))

    monkeypatch.setattr(
        "quantbot.paper_core.context_learning.consider_context_learning",
        lambda store, broker, *, now: calls.append(now),
    )
    result = await daily_consideration(
        SimpleNamespace(clocks=SimpleNamespace(sample=lambda: (NOW, 0))),
        SimpleNamespace(learning_calendar=calendar),
        SimpleNamespace(learning_origin=NOW),
    )
    assert calls == [NOW] and result == ("NO_COMPLETED_EXCHANGE_SESSION",)


@pytest.mark.asyncio
async def test_outcome_bridge_retries_pending_correction_without_new_revision(tmp_path):
    from sqlalchemy import update

    from quantbot.paper_core.context_learning import _core_batch
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.outcomes import process_outcomes
    from quantbot.paper_core.store import table
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        decision = await trader_cycle(store, broker, observation, model, proof, now=broker.now)
        broker.now += timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        process_outcomes(store, cutoff=broker.now)
        progress = {"decisions": 0, "outcomes": 0}
        _, _, trips, objections, _, revisions = _core_batch(store, progress, now=broker.now)
        assert len(trips) == 1 and objections[trips[0].id] == ()
        work = table("core_outcome_work")
        with db.transaction() as session:
            session.execute(
                update(work).where(work.c.lot_id == decision.id).values(version=work.c.version + 1)
            )
        progress.update(outcomes=revisions[-1][0], pending_lots=[decision.id])
        _, _, trips, objections, _, revisions = _core_batch(store, progress, now=broker.now)
        assert not revisions and len(trips) == 1
        assert "CONTEXT_OUTCOME_CORRECTION_PENDING" in objections[trips[0].id]
        with db.transaction() as session:
            session.execute(
                update(work)
                .where(work.c.lot_id == decision.id)
                .values(processed_version=work.c.version)
            )
        _, _, trips, objections, _, revisions = _core_batch(store, progress, now=broker.now)
        assert not revisions and objections[trips[0].id] == ()
    finally:
        db.close()
