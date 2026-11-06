"""Real registry adapters use synthetic stores and no external data."""

from datetime import timedelta

import pytest

from quantbot.research.manifest import content_id
from quantbot.storage import Database
from tests.unit.learner.test_context_feedback import review
from tests.unit.learner.test_context_integration import news, stores
from tests.unit.paper_core.test_external_context import NOW, permissions


def setup_plan(tmp_path):
    from quantbot.learner.context_admission import ContextEvaluationPlan, register_context_plan

    research, experiments = stores(tmp_path)
    registry = tmp_path / "synthetic-registry.db"
    Database.create(registry).close()
    plan = ContextEvaluationPlan(
        experiment_id="synthetic-context",
        recorded_at=NOW - timedelta(days=1),
        manifest_hash=experiments.state("synthetic-context").manifest_hash,
        source_start=NOW - timedelta(days=1),
        training_start=NOW,
        split_at=NOW + timedelta(days=10),
        evaluation_end=NOW + timedelta(days=20),
    )
    register_context_plan(experiments, plan, now=plan.recorded_at)
    return research, experiments, registry, plan


def test_real_adapter_charges_before_fit_and_reuses_frozen_evaluation(tmp_path, monkeypatch):
    from quantbot.learner.context_admission import (
        CanonicalContextAdmission,
        run_registered_context_evaluation,
    )
    from quantbot.learner.context_feedback import fit_interpreter_candidate
    from quantbot.learner.context_integration import (
        journal_context_snapshot,
        load_retained_candidate,
    )
    from quantbot.paper_core.context_integration import capture_research_context

    research, experiments, registry, plan = setup_plan(tmp_path)
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
        assert snapshot is not None
        journal_context_snapshot(
            experiments,
            research,
            experiment_id=plan.experiment_id,
            snapshot_id=snapshot.snapshot_id,
            recorded_at=at,
        )
        experiments.append(review(snapshot.readings[0]), idempotency_key=f"review:{index}")
    adapter = CanonicalContextAdmission(
        plan, registry_path=registry, declaration_path=registry, now=plan.evaluation_end
    )

    def checked_fit(*args, **kwargs):
        assert adapter.used_calls(plan.experiment_id) == 1
        return fit_interpreter_candidate(*args, **kwargs)

    monkeypatch.setattr(
        "quantbot.learner.context_integration.fit_interpreter_candidate", checked_fit
    )
    result = run_registered_context_evaluation(
        experiments,
        research,
        experiment_id=plan.experiment_id,
        registry_path=registry,
        declaration_path=registry,
        now=plan.evaluation_end + timedelta(days=1),
    )
    assert result.status == "EVALUATED" and result.candidate_id
    candidate = load_retained_candidate(
        experiments,
        experiment_id=plan.experiment_id,
        candidate_id=result.candidate_id,
        at=plan.evaluation_end + timedelta(days=1),
    )
    assert candidate.recorded_at == plan.evaluation_end + timedelta(days=1)
    assert plan.plan_id in candidate.lineage_refs and result.evaluation_plan_id == plan.plan_id
    again = run_registered_context_evaluation(
        experiments,
        research,
        experiment_id=plan.experiment_id,
        registry_path=registry,
        declaration_path=registry,
        now=plan.evaluation_end + timedelta(days=2),
    )
    assert again == result and adapter.used_calls(plan.experiment_id) == 1


def test_plan_is_predeclared_and_no_result_churn_for_unchanged_missing_evidence(tmp_path):
    from quantbot.learner.context_admission import (
        ContextEvaluationPlan,
        register_context_plan,
        run_registered_context_evaluation,
    )

    research, experiments, registry, plan = setup_plan(tmp_path)
    with pytest.raises(ValueError, match="predeclared"):
        ContextEvaluationPlan.model_validate(plan.model_dump() | {"recorded_at": plan.split_at})
    with pytest.raises(ValueError, match="ALREADY_REGISTERED"):
        register_context_plan(
            experiments,
            plan.model_copy(update={"split_at": plan.split_at + timedelta(days=1)}),
            now=plan.recorded_at,
        )
    with pytest.raises(ValueError, match="NOT_REACHED"):
        run_registered_context_evaluation(
            experiments,
            research,
            experiment_id=plan.experiment_id,
            registry_path=registry,
            declaration_path=registry,
            now=plan.split_at,
        )
    results = [
        run_registered_context_evaluation(
            experiments,
            research,
            experiment_id=plan.experiment_id,
            registry_path=registry,
            declaration_path=registry,
            now=plan.evaluation_end + timedelta(days=index),
        )
        for index in range(3)
    ]
    assert results[0] == results[1] == results[2]
    assert (
        sum(
            e.kind == "ContextEvaluation"
            for e in experiments.read_as_of(
                plan.experiment_id, plan.evaluation_end + timedelta(days=3)
            )
        )
        == 1
    )


def test_real_registry_guard_refuses_consumed_protected_and_outside_declared_ranges(tmp_path):
    import sqlite3

    from quantbot.learner.context_admission import CanonicalContextAdmission

    _, _, registry, plan = setup_plan(tmp_path)
    adapter = CanonicalContextAdmission(
        plan, registry_path=registry, declaration_path=registry, now=plan.evaluation_end
    )
    assert adapter.permits(plan.source_start, plan.evaluation_end)
    assert not adapter.permits(plan.source_start - timedelta(days=1), plan.evaluation_end)
    with sqlite3.connect(registry) as db:
        db.execute(
            "insert into hypothesis_data_windows values(?,?,?,?,?,?,?,?,?)",
            (
                "synthetic-protected",
                1,
                "us-equities-daily",
                "PROTECTED_EVALUATION",
                plan.source_start.date().isoformat(),
                plan.evaluation_end.date().isoformat(),
                "CONSUMED",
                None,
                plan.recorded_at.isoformat(),
            ),
        )
    assert not adapter.permits(plan.source_start, plan.evaluation_end)


def test_context_and_price_fits_share_real_six_attempt_budget(tmp_path):
    from quantbot.learner.context_admission import CanonicalContextAdmission
    from quantbot.learner.experiment_research import _trial_prefix
    from quantbot.learner.forward import _REGISTRY, _registry_session
    from quantbot.research.intraday_accounting import record_intraday_attempt

    _, _, registry, plan = setup_plan(tmp_path)
    token = _REGISTRY.set(registry)
    try:
        with _registry_session(write=True) as session:
            for index in range(6):
                record_intraday_attempt(
                    session,
                    search_id=_trial_prefix(plan.experiment_id) + str(index),
                    configuration_hash="a" * 64,
                    start=plan.source_start.date(),
                    end=plan.evaluation_end.date(),
                    now=plan.evaluation_end,
                )
    finally:
        _REGISTRY.reset(token)
    adapter = CanonicalContextAdmission(
        plan, registry_path=registry, declaration_path=registry, now=plan.evaluation_end
    )
    assert adapter.used_calls(plan.experiment_id) == 6
    with pytest.raises(ValueError, match="SIX_CALL"):
        adapter.charge(
            "intraday:context-" + content_id(plan.experiment_id)[:16] + ":" + "b" * 64,
            plan.manifest_hash,
        )


def test_registry_charge_survives_new_adapter_and_prevents_holdout_retries(tmp_path):
    from quantbot.learner.context_admission import CanonicalContextAdmission

    _, _, registry, plan = setup_plan(tmp_path)
    prefix = "intraday:context-" + content_id(plan.experiment_id)[:16] + ":"
    adapter = CanonicalContextAdmission(
        plan, registry_path=registry, declaration_path=registry, now=plan.evaluation_end
    )
    adapter.charge(prefix + "a" * 64, plan.manifest_hash)
    recreated = CanonicalContextAdmission(
        plan,
        registry_path=registry,
        declaration_path=registry,
        now=plan.evaluation_end + timedelta(days=1),
    )
    with pytest.raises(ValueError, match="ALREADY_RESERVED"):
        recreated.charge(prefix + "a" * 64, plan.manifest_hash)
    with pytest.raises(ValueError, match="FOLD_ALREADY_SPENT"):
        recreated.charge(prefix + "b" * 64, plan.manifest_hash)
    assert recreated.used_calls(plan.experiment_id) == 1
