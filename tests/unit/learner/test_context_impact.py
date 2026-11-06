"""Paired research engines spend the existing canonical burden before measurement."""

from datetime import timedelta
from decimal import Decimal

import pytest

from quantbot.research.manifest import content_id
from tests.unit.learner.test_context_admission import setup_plan


def impact_setup(tmp_path):
    from quantbot.learner.context_impact import CanonicalImpactAdmission, ContextImpactPlan

    _, _, registry, source = setup_plan(tmp_path)
    plan = ContextImpactPlan(
        experiment_id=source.experiment_id,
        manifest_hash=source.manifest_hash,
        recorded_at=source.recorded_at,
        candidate_id="a" * 64,
        source_start=source.source_start,
        training_start=source.training_start,
        split_at=source.split_at,
        evaluation_end=source.evaluation_end,
        source_policy_id="b" * 64,
        modeled_cost_contract_id="d" * 64,
    )
    return (
        registry,
        plan,
        CanonicalImpactAdmission(
            plan, registry_path=registry, declaration_path=registry, now=plan.evaluation_end
        ),
    )


def test_two_charges_commit_and_frozen_failed_fold_cannot_repeat(tmp_path):
    from quantbot.learner.context_admission import _session
    from quantbot.research.intraday_accounting import verified_intraday_attempt_ids

    registry, plan, admission = impact_setup(tmp_path)
    identities = admission.charge_pair("c" * 64)
    with _session(registry) as session:
        assert set(identities) <= verified_intraday_attempt_ids(session)
    assert admission.used_calls() == 2
    # A crashed engine or changed evidence never licenses another look at the same fold.
    with pytest.raises(ValueError, match="FOLD_ALREADY_SPENT"):
        admission.charge_pair("d" * 64)
    assert admission.used_calls() == 2


@pytest.mark.parametrize("prior,allowed", [(4, True), (5, False)])
def test_pair_reservation_is_atomic_at_shared_six_call_limit(tmp_path, prior, allowed):
    from quantbot.learner.context_admission import _session
    from quantbot.learner.experiment_research import _trial_prefix
    from quantbot.research.intraday_accounting import record_intraday_attempt

    registry, plan, admission = impact_setup(tmp_path)
    with _session(registry, write=True) as session:
        for index in range(prior):
            record_intraday_attempt(
                session,
                search_id=_trial_prefix(plan.experiment_id) + str(index),
                configuration_hash=content_id(index),
                start=plan.source_start.date(),
                end=plan.evaluation_end.date(),
                now=plan.recorded_at,
            )
    if allowed:
        admission.charge_pair("c" * 64)
        assert admission.used_calls() == 6
    else:
        with pytest.raises(ValueError, match="SIX_CALL"):
            admission.charge_pair("c" * 64)
        assert admission.used_calls() == 5


def test_impact_plan_requires_prospective_training_and_frozen_costs(tmp_path):
    from quantbot.learner.context_impact import ContextImpactPlan

    _, plan, _ = impact_setup(tmp_path)
    with pytest.raises(ValueError):
        ContextImpactPlan.model_validate(plan.model_dump() | {"stock_round_trip_bps": 0})
    with pytest.raises(ValueError):
        ContextImpactPlan.model_validate(
            plan.model_dump() | {"training_start": plan.recorded_at - timedelta(seconds=1)}
        )


def test_predeclared_over_200_sampling_uses_time_and_id_never_net(tmp_path):
    from quantbot.learner.context_impact import select_impact_feedback

    _, plan, admission = impact_setup(tmp_path)
    assert plan.sampling_policy == "FIRST100_PER_SPLIT_SIDE_BY_DECISION_TIME_ID_V1"
    assert plan.source_cluster_policy == "EXCLUDE_HELDOUT_TRAINING_SOURCE_OVERLAP_NO_REFILL_V1"

    class Row:
        def __init__(self, index, side):
            self.decision_id = f"{side}:{index:04d}"
            self.decision_at = plan.training_start if side == "train" else plan.split_at
            self.label_available_at = self.decision_at + timedelta(seconds=1)

        @property
        def net_return_bps(self):
            raise AssertionError("Sampling must never inspect economic results")

    rows = [Row(index, side) for side in ("train", "heldout") for index in range(150)]
    sampled = select_impact_feedback(tuple(reversed(rows)), plan)
    expected = rows[:100] + rows[150:250]
    assert [r.decision_id for r in sampled] == [r.decision_id for r in expected]
    assert select_impact_feedback(tuple(rows), plan) == sampled
    assert admission.used_calls() == 0


def test_predeclared_purge_removes_overlapping_heldout_cluster_without_refill(tmp_path):
    from types import SimpleNamespace

    from quantbot.learner.context_impact import purge_heldout_source_overlap

    _, plan, _ = impact_setup(tmp_path)
    rows = tuple(
        SimpleNamespace(
            decision_id=str(index),
            decision_at=at,
            context=SimpleNamespace(used_document_ids=(ref,)),
        )
        for index, (at, ref) in enumerate(
            (
                (plan.training_start, "training"),
                (plan.split_at, "revised-training-source"),
                (plan.split_at, "independent-source"),
            )
        )
    )
    documents = {
        "training": SimpleNamespace(source_id="original-cluster"),
        "revised-training-source": SimpleNamespace(source_id="original-cluster"),
        "independent-source": SimpleNamespace(source_id="independent-cluster"),
    }
    assert purge_heldout_source_overlap(rows, documents, split_at=plan.split_at) == (
        rows[0],
        rows[2],
    )


@pytest.mark.parametrize("copies_per_day", [1, 20])
def test_retained_prospective_candidate_outcomes_reach_two_charged_real_fits(
    tmp_path,
    monkeypatch,
    copies_per_day,
):
    from quantbot.learner.context_admission import _session
    from quantbot.learner.context_feedback import (
        build_examples,
        candidate_polarity,
        fit_interpreter_candidate,
    )
    from quantbot.learner.context_impact import (
        CanonicalImpactAdmission,
        ContextImpactPlan,
        load_retained_impact_comparison,
        register_context_impact_plan,
        run_registered_downstream_comparison,
    )
    from quantbot.learner.context_runtime_feedback import (
        ContextShadowConsumption,
        ModeledCostContract,
        retain_shadow_consumption,
    )
    from quantbot.learner.five_minute import FEATURE_NAMES
    from quantbot.paper_core.context_integration import (
        load_context_capture,
        load_source_permissions,
        retained_context_documents,
    )
    from quantbot.paper_core.external_context import build_context_snapshot
    from quantbot.paper_core.store import put
    from tests.unit.learner import test_context_runtime_feedback as fixture
    from tests.unit.learner.test_context_feedback import review
    from tests.unit.learner.test_context_feedback import rows as reading_rows
    from tests.unit.learner.test_context_integration import news
    from tests.unit.paper_core.test_external_context import NOW, permissions

    research, experiments, registry, _ = setup_plan(tmp_path)
    source = reading_rows()
    examples, _ = build_examples(
        tuple(s for _, s in source),
        tuple(d for d, _ in source),
        tuple(review(s.readings[0]) for _, s in source),
        as_of=NOW + timedelta(days=20),
    )
    candidate = fit_interpreter_candidate(
        examples, split_at=NOW + timedelta(days=10), as_of=NOW + timedelta(days=20)
    )
    experiments.append(candidate, idempotency_key="synthetic-prior-candidate")
    contract = ModeledCostContract(
        recorded_at=NOW + timedelta(days=20), source_policy_refs=("synthetic-policy",)
    )
    with research.connection() as db:
        research._set(db, "context:source-permissions", [permissions().model_dump(mode="json")])
    plan = ContextImpactPlan(
        experiment_id="synthetic-context",
        recorded_at=NOW + timedelta(days=20),
        manifest_hash=experiments.state("synthetic-context").manifest_hash,
        candidate_id=candidate.candidate_id,
        source_policy_id=content_id(load_source_permissions(research)),
        source_start=NOW + timedelta(days=21),
        training_start=NOW + timedelta(days=22),
        split_at=NOW + timedelta(days=38),
        evaluation_end=NOW + timedelta(days=46),
        modeled_cost_contract_id=contract.contract_id,
    )
    assert register_context_impact_plan(experiments, plan, now=plan.recorded_at)
    assert not register_context_impact_plan(experiments, plan, now=plan.recorded_at)
    monkeypatch.setattr(fixture, "stores", lambda _: (research, experiments))
    for index in range(12):
        day = 22 + index * 2
        monkeypatch.setattr(fixture, "NOW", NOW + timedelta(days=day))
        monkeypatch.setattr(fixture, "news", lambda state, day=day: news(state, day))
        evidence = fixture.evidence(tmp_path)
        observation = evidence[3].model_copy(
            update={"features": tuple((name, 0.01 * (index + 1)) for name in FEATURE_NAMES)}
        )
        decision = evidence[4].model_copy(update={"observation_id": observation.id})
        trip = evidence[5].model_copy(update={"decision_id": decision.id, "lot_id": f"lot:{index}"})
        capture = load_context_capture(research, observation.context_snapshot_id)
        at = decision.recorded_at_utc
        shadow = build_context_snapshot(
            retained_context_documents(research, capture),
            symbol="AAPL",
            at=at,
            coverage=capture.snapshot.coverage,
            used_document_ids=capture.snapshot.eligible_document_ids,
        )
        predictions = tuple(
            (r.reading_id, candidate_polarity(candidate, r, at=at))
            for r in shadow.readings
            if r.document_id in shadow.used_document_ids
        )
        values = [value for _, value in predictions]
        positive, negative = sum(v > 0 for v in values), sum(v < 0 for v in values)
        receipt = ContextShadowConsumption(
            observation_id=observation.id,
            original_snapshot_id=capture.snapshot.snapshot_id,
            consumed_at=at,
            recorded_at=at,
            shadow=shadow,
            candidate_id=candidate.candidate_id,
            reading_predictions=predictions,
            candidate_feature_values=(
                float(len(values)),
                float(positive),
                float(negative),
                sum(values) / len(values),
                float(bool(positive and negative)),
                shadow.features[-1][1],
            ),
        )
        day_observations, day_decisions, day_trips = [], [], []
        with _session(registry, write=True) as session:
            for copy in range(copies_per_day):
                observation = observation.model_copy(update={"sequence": copy + 1})
                decision = decision.model_copy(update={"observation_id": observation.id})
                trip = trip.model_copy(
                    update={
                        "decision_id": decision.id,
                        "lot_id": f"lot:{index}:{copy}",
                    }
                )
                consumed = receipt.model_copy(update={"observation_id": observation.id})
                assert retain_shadow_consumption(
                    research,
                    consumed,
                    now=at,
                    candidate_validator=lambda value: (
                        value.reading_predictions
                        == tuple(
                            (r.reading_id, candidate_polarity(candidate, r, at=value.consumed_at))
                            for r in value.shadow.readings
                            if r.document_id in value.shadow.used_document_ids
                        )
                    ),
                )
                put(session, "core_observations", observation.id, observation)
                day_observations.append(observation)
                day_decisions.append(decision)
                day_trips.append(trip)
        result = fixture.run(
            (research, experiments, evidence[2], observation, decision, trip),
            modeled_cost_contract=contract,
            observations=day_observations,
            decisions=day_decisions,
            round_trips=day_trips,
            outcome_objections={item.id: () for item in day_trips},
        )
        assert result.feedback_written == result.modeled_labels_written == copies_per_day
    admission = CanonicalImpactAdmission(
        plan, registry_path=registry, declaration_path=registry, now=plan.evaluation_end
    )
    import quantbot.learner.context_feedback as implementation

    real_fit = implementation.fit_estimator
    calls = []

    def charged_fit(*args, **kwargs):
        assert admission.used_calls() == 2
        calls.append(args[2])
        return real_fit(*args, **kwargs)

    monkeypatch.setattr(implementation, "fit_estimator", charged_fit)
    args = dict(
        experiment_id=plan.experiment_id,
        candidate_id=candidate.candidate_id,
        registry_path=registry,
        declaration_path=registry,
        now=plan.evaluation_end,
    )
    result = run_registered_downstream_comparison(experiments, research, **args)
    expected_train, expected_heldout = min(100, 8 * copies_per_day), min(100, 4 * copies_per_day)
    assert result.status == "EVALUATED"
    assert result.eligible_examples == expected_train + expected_heldout
    assert len(calls) == 2 and admission.used_calls() == 2
    assert result.selected_heldout_count == 0  # All genuine synthetic net outcomes lost.
    retained = load_retained_impact_comparison(
        experiments,
        experiment_id=plan.experiment_id,
        comparison_id=result.comparison_id,
        at=plan.evaluation_end,
    )
    assert len(retained.comparison.training_decision_ids) == expected_train
    assert len(retained.comparison.heldout_decision_ids) == expected_heldout
    assert retained.cost_basis == "MODELED_NET" and not retained.grants_activation
    assert run_registered_downstream_comparison(experiments, research, **args) == result
    assert len(calls) == 2 and admission.used_calls() == 2
    suspended_at = plan.evaluation_end + timedelta(microseconds=1)
    fixture.run(
        (research, experiments, evidence[2], observation, decision, trip),
        outcome_objections={trip.id: ("CONTEXT_OUTCOME_CORRECTION_PENDING",)},
        as_of=suspended_at,
    )
    with pytest.raises(ValueError, match="SELECTED_OUTCOMES_UNVERIFIED"):
        load_retained_impact_comparison(
            experiments,
            experiment_id=plan.experiment_id,
            comparison_id=result.comparison_id,
            at=suspended_at,
        )
    with pytest.raises(ValueError, match="SELECTED_OUTCOMES_UNVERIFIED"):
        run_registered_downstream_comparison(
            experiments, research, **(args | {"now": suspended_at})
        )
    assert len(calls) == 2 and admission.used_calls() == 2
    fixture.run(
        (research, experiments, evidence[2], observation, decision, trip),
        as_of=suspended_at + timedelta(microseconds=1),
    )
    from quantbot.learner.context_integration import load_retained_candidate
    from quantbot.learner.context_runtime_feedback import load_runtime_context_feedback

    feedback, _ = load_runtime_context_feedback(
        experiments,
        research,
        experiment_id=plan.experiment_id,
        as_of=plan.evaluation_end,
    )
    assert feedback[0].feedback_id in retained.cost_evidence_ids
    assert feedback[0].outcome_id in retained.cost_evidence_ids
    assert contract.contract_id in retained.cost_evidence_ids
    later = plan.evaluation_end + timedelta(seconds=1)
    revised = trip.model_copy(
        update={
            "revision": 2,
            "supersedes": trip.id,
            "recorded_at_utc": later,
            "available_at": later,
            "source_state_hash": "corrected-source",
            "gross_pnl": Decimal(-2),
        }
    )
    correction = fixture.run(
        (research, experiments, evidence[2], observation, decision, revised),
        modeled_cost_contract=contract,
    )
    assert correction.feedback_written == correction.modeled_labels_written == 1
    assert (
        load_retained_candidate(
            experiments,
            experiment_id=plan.experiment_id,
            candidate_id=candidate.candidate_id,
            at=later,
        )
        == candidate
    )
    with pytest.raises(ValueError, match="IMPACT_COMPARISON_INVALIDATED"):
        load_retained_impact_comparison(
            experiments,
            experiment_id=plan.experiment_id,
            comparison_id=result.comparison_id,
            at=later,
        )
    assert admission.used_calls() == 2
