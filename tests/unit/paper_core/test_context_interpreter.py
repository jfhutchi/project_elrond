"""Prospective context is separate from observations and can only veto entry."""

from datetime import timedelta

import pytest
from tests.fixtures.core_m2 import prepare
from tests.unit.learner.test_context_feedback import review
from tests.unit.learner.test_context_integration import news
from tests.unit.paper_core.test_context_learning import fixture
from tests.unit.paper_core.test_external_context import NOW, permissions

from quantbot.learner.context_runtime_feedback import ModeledCostContract, load_shadow_consumption
from quantbot.paper_core.context_integration import capture_research_context, load_context_capture
from quantbot.paper_core.context_interpreter import (
    POINTER_KEY,
    _publish,
    attach_context_interpreter,
    change_interpreter_mode,
    current_version,
    filter_objections,
    forecast_spec_id,
    learning_binding,
)
from quantbot.paper_core.context_learning import BINDING_KEY, consider_context_learning
from quantbot.paper_core.contracts import Observation, ObservationContextFilterInput
from quantbot.paper_core.trader import evaluate_entry
from quantbot.research.manifest import content_id


def observation(snapshot_id):
    return Observation(
        symbol="AAPL",
        recorded_at_utc=NOW,
        source_start=NOW - timedelta(minutes=5),
        source_end=NOW,
        available_at=NOW,
        price=100,
        raw_data_digest="explicit-test",
        features=(("return", 0.01),),
        dependencies=(),
        dependency_available_at=(),
        sequence=1,
        session_close=NOW + timedelta(hours=6),
        context_snapshot_id=snapshot_id,
    )


def test_shadow_is_prospective_immutable_and_restart_idempotent(tmp_path):
    _, _, _, research, _, _, _ = fixture(tmp_path)
    news(research)
    snapshot = capture_research_context(
        research, symbol="AAPL", at=NOW, permissions={"alpaca-news": permissions()}
    )
    original = observation(snapshot.snapshot_id)
    attached = attach_context_interpreter(research, original, now=NOW)
    assert attached == original and attached.context_filter is None
    receipt = load_shadow_consumption(research, observation_id=attached.id)
    assert receipt and receipt.consumed_at == NOW and receipt.shadow.used_document_ids
    assert "fixed_baseline_id" not in receipt.model_dump()
    assert "fixed_feature_values" not in receipt.model_dump()
    assert receipt.grants_execution is False
    assert load_context_capture(research, snapshot.snapshot_id).snapshot.used_document_ids == ()
    again = attach_context_interpreter(research, original, now=NOW + timedelta(seconds=15))
    assert again == original
    assert load_shadow_consumption(research, observation_id=again.id) == receipt


def test_invalid_current_pointer_never_bypasses_to_baseline(tmp_path):
    _, _, _, research, _, _, _ = fixture(tmp_path)
    with research.connection() as db:
        research._set(db, POINTER_KEY, "missing")
        research._set(db, "context:interpreter-history", ["missing"])
    attached = attach_context_interpreter(research, observation(None), now=NOW)
    assert attached.context_filter and attached.context_filter.objection
    assert filter_objections(research, attached, now=NOW)


def test_source_invalidation_rejects_shadow_without_inventing_used_receipt(tmp_path):
    from quantbot.learner.context_feedback import SourceInvalidation
    from quantbot.learner.context_integration import ContextInvalidationRecord

    _, _, _, research, experiments, _, _ = fixture(tmp_path)
    source = news(research)
    snapshot = capture_research_context(
        research, symbol="AAPL", at=NOW, permissions={"alpaca-news": permissions()}
    )
    experiments.append(
        ContextInvalidationRecord(
            experiment_id="synthetic-context",
            recorded_at=NOW,
            invalidation=SourceInvalidation(
                reference_id=source, invalidated_at=NOW, reason="DELETED"
            ),
        ),
        idempotency_key="invalidate-test",
    )
    attached = attach_context_interpreter(research, observation(snapshot.snapshot_id), now=NOW)
    assert attached.context_filter and "SOURCE_INVALIDATED" in attached.context_filter.objection
    assert load_shadow_consumption(research, observation_id=attached.id) is None


@pytest.mark.parametrize("fixed_first", [False, True])
def test_auto_shadow_admission_uses_real_candidate_and_future_plan(
    tmp_path, monkeypatch, fixed_first
):
    _, store, broker, research, experiments, plan, _ = fixture(tmp_path)
    binding = learning_binding(research)
    contract = ModeledCostContract(
        recorded_at=binding.recorded_at,
        source_policy_refs=(binding.manifest_hash, "explicit-synthetic-crypto-policy"),
        crypto_round_trip_bps=60 if fixed_first else 50,
        cost_scope="ROUND_TRIP_FEES_PLUS_SLIPPAGE_V2" if fixed_first else None,
    )
    binding = binding.model_copy(
        update={
            "interpreter_admission": "FIXED_LEXICAL_OUTCOME_SHADOW_V1"
            if fixed_first
            else "SHADOW_ONLY_ON_BETTER_READING_HOLDOUT_V1",
            "modeled_cost_contract_id": contract.contract_id,
            "registry_path": store.database.path,
        }
    )
    with research.connection() as db:
        research._receipt(db, contract)
        research._set(db, BINDING_KEY, binding)
    initial = None
    if fixed_first:
        started = consider_context_learning(
            store, broker, now=NOW - timedelta(minutes=1), force=True
        )
        initial = current_version(research)
        assert initial.fixed_baseline_id and initial.candidate_id is None
        assert started.impact_evaluation_end == plan.evaluation_end
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
        experiments.append(
            review(snapshot.readings[0], polarity=-0.25), idempotency_key=f"review-{index}"
        )
        consider_context_learning(store, broker, now=at, force=True)
    status = consider_context_learning(store, broker, now=plan.evaluation_end, force=True)
    version = current_version(research)
    assert version and version.mode == "SHADOW_ONLY" and status.interpreter_mode == "SHADOW_ONLY"
    if initial:
        assert version.previous_version_id == initial.version_id
        assert version.fixed_baseline_id is None and version.candidate_id is not None
    assert status.impact_evaluation_end > plan.evaluation_end + timedelta(days=14)
    news(research, 21)
    now = NOW + timedelta(days=21)
    snapshot = capture_research_context(
        research,
        symbol="AAPL",
        at=now,
        permissions={"alpaca-news": permissions()},
        maximum_documents=1,
    )
    original = observation(snapshot.snapshot_id).model_copy(update={"recorded_at_utc": now})
    attached = attach_context_interpreter(research, original, now=now)
    assert attached.context_filter is None
    receipt = load_shadow_consumption(research, observation_id=attached.id)
    assert receipt.candidate_id == version.candidate_id
    assert receipt.candidate_feature_values[3] == pytest.approx(-0.25)
    assert dict(receipt.shadow.features)["polarity_mean"] == 1
    assert current_version(research) == version
    # A pending canonical correction must revoke a retained trading artifact even
    # when this observation has no eligible news. Reuse the caller's locked snapshot.
    trading = version.model_copy(
        update={"mode": "TRADING_FILTER", "impact_comparison_id": "c" * 64}
    )
    with store.database.transaction() as locked, monkeypatch.context() as patch:
        from sqlalchemy import text

        locked.execute(text("BEGIN IMMEDIATE"))
        checks = []

        def pending_comparison(*args, core_store=None, session=None, **kwargs):
            assert core_store is store and session is locked
            checks.append((core_store, session))
            raise ValueError("CONTEXT_OUTCOME_CORRECTION_PENDING")

        patch.setattr("quantbot.paper_core.context_interpreter.current_version", lambda _: trading)
        patch.setattr(
            "quantbot.paper_core.context_interpreter.load_retained_impact_comparison",
            pending_comparison,
        )
        reasons = filter_objections(
            research, observation(None), now=now, core_store=store, session=locked
        )
        assert reasons == ("CONTEXT_FILTER_UNVERIFIED:CONTEXT_OUTCOME_CORRECTION_PENDING",)
        assert len(checks) == 1
        operational = observation(None).model_copy(update={"evidence_lane": "BROKER_PAPER"})
        assert filter_objections(research, operational, now=now) == (
            "CONTEXT_FILTER_UNVERIFIED:CURRENT_OUTCOME_REVALIDATION_REQUIRED",
        )
        refused = attach_context_interpreter(research, operational, now=now)
        assert "CURRENT_OUTCOME_REVALIDATION_REQUIRED" in refused.context_filter.objection
        assert len(checks) == 1
    with pytest.raises(ValueError, match="CAS_CONFLICT"):
        _publish(research, version, None)
    # Mode changes use the canonical ledger writer lock and exact current runtime refs.
    from types import SimpleNamespace

    from quantbot.learner.context_impact import ContextImpactEvaluation
    from quantbot.paper_core.context_interpreter import change_interpreter_mode

    monkeypatch.setattr(
        "quantbot.paper_core.authority.commission_in_session",
        lambda *args: SimpleNamespace(id="commission"),
    )
    monkeypatch.setattr(
        "quantbot.paper_core.activation.active_in_session",
        lambda *args: (None, SimpleNamespace(id="price-activation")),
    )
    monkeypatch.setattr(
        "quantbot.paper_core.authority.current_runtime_event",
        lambda *args: SimpleNamespace(action="RESUME_ENTRY"),
    )
    args = dict(
        expected_version_id=version.version_id,
        candidate_id=version.candidate_id,
        semantic_evaluation_id=version.semantic_evaluation_id,
        commission_id="commission",
        price_activation_id="price-activation",
        approval_ref="synthetic-operator-review",
        now=now,
    )
    with pytest.raises(ValueError, match="EXACT_FENCED"):
        change_interpreter_mode(store, research, mode="DISABLED", **args)
    monkeypatch.setattr(
        "quantbot.paper_core.authority.current_runtime_event",
        lambda *args: SimpleNamespace(action="FENCE_ENTRY"),
    )
    with pytest.raises(ValueError, match="EXACT_IMPACT"):
        change_interpreter_mode(store, research, mode="TRADING_FILTER", **args)
    impact = ContextImpactEvaluation(
        experiment_id=binding.experiment_id,
        recorded_at=now,
        candidate_id=version.candidate_id,
        plan_id=version.impact_plan_id,
        request_id="c" * 64,
        cost_basis="MODELED_NET",
        status="EVALUATED",
        reason="explicit-insufficient-fixture",
        comparison_id="d" * 64,
        selected_heldout_count=3,
        selected_heldout_mean_net_bps=10,
        eligible_examples=12,
    )
    experiments.append(impact, idempotency_key="insufficient-promotion")
    with pytest.raises(ValueError, match="COST_AWARE_IMPROVEMENT"):
        change_interpreter_mode(
            store, research, mode="TRADING_FILTER", impact_evaluation_id=content_id(impact), **args
        )
    apparently_eligible = impact.model_copy(update={"selected_heldout_count": 4})
    experiments.append(apparently_eligible, idempotency_key="pending-promotion")
    with monkeypatch.context() as patch:

        def pending_promotion(*args, core_store=None, session=None, **kwargs):
            assert core_store is store and session is not None and session.in_transaction()
            raise ValueError("CONTEXT_OUTCOME_CORRECTION_PENDING")

        patch.setattr(
            "quantbot.paper_core.context_interpreter.load_retained_impact_comparison",
            pending_promotion,
        )
        with pytest.raises(ValueError, match="CORRECTION_PENDING"):
            change_interpreter_mode(
                store,
                research,
                mode="TRADING_FILTER",
                impact_evaluation_id=content_id(apparently_eligible),
                **args,
            )
    assert current_version(research) == version
    disabled = change_interpreter_mode(store, research, mode="DISABLED", **args)
    assert disabled.previous_version_id == version.version_id and disabled.mode == "DISABLED"
    with pytest.raises(ValueError, match="EXPECTED_VERSION"):
        change_interpreter_mode(store, research, mode="DISABLED", **args)


@pytest.mark.parametrize("prediction", [-1.0, 1.0])
def test_overlay_never_removes_price_or_risk_wait_and_links_forecast(tmp_path, prediction):
    db, store, _, proof, original, model = prepare(tmp_path)
    try:
        bad = original.model_copy(update={"symbol": "UNCOMMISSIONED"})
        before = evaluate_entry(store.config, bad, model, proof)
        overlay = ObservationContextFilterInput(
            forecast_spec_id=forecast_spec_id(bad),
            forecast_id="explicit-test-forecast",
            version_id="test",
            expected_net_bps=prediction,
        )
        attached = bad.model_copy(update={"context_filter": overlay})
        result = evaluate_entry(store.config, attached, model, proof)
        assert before.action == result.action == "WAIT"
        assert set(before.gates) <= set(result.gates)
        assert result.context_forecast_id == overlay.forecast_id
        assert forecast_spec_id(attached) == forecast_spec_id(bad)
        assert attached.id != bad.id
        assert ("CONTEXT_PREDICTED_NET_NONPOSITIVE" in result.gates) == (prediction <= 0)
        assert evaluate_entry(store.config, original, model, proof).action == "BUY"
        eligible = original.model_copy(update={"context_filter": overlay})
        assert evaluate_entry(store.config, eligible, model, proof).action == (
            "WAIT" if prediction <= 0 else "BUY"
        )
    finally:
        db.close()


def test_mode_change_rejects_noncanonical_registry_and_preserves_pointer(tmp_path):
    _, store, _, research, _, _, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match="CANONICAL_LEDGER"):
        change_interpreter_mode(
            store,
            research,
            expected_version_id="x",
            candidate_id="y",
            semantic_evaluation_id="z",
            commission_id="c",
            price_activation_id="a",
            approval_ref="explicit-test",
            now=NOW,
            mode="DISABLED",
        )
    assert current_version(research) is None


def test_legacy_context_artifacts_retain_exact_serialized_identity():
    from quantbot.learner.context_impact import ContextImpactEvaluation, ContextImpactPlan
    from quantbot.paper_core.context_interpreter import ContextForecast, ContextInterpreterVersion

    contract = ModeledCostContract(recorded_at=NOW, source_policy_refs=("old-policy",))
    assert "cost_scope" not in contract.model_dump()
    assert contract.contract_id == content_id(
        dict(
            recorded_at=NOW,
            source_policy_refs=("old-policy",),
            stock_round_trip_bps=10,
            crypto_round_trip_bps=50,
            interpretation="RESEARCH_ESTIMATE_NOT_ACTUAL_FEE",
        )
    )
    version = ContextInterpreterVersion(
        binding_id="binding",
        recorded_at=NOW,
        mode="SHADOW_ONLY",
        candidate_id="a" * 64,
        semantic_evaluation_id="semantic",
        impact_plan_id="impact",
        source_policy_id="policy",
    )
    plan = ContextImpactPlan(
        experiment_id="synthetic",
        recorded_at=NOW,
        manifest_hash="b" * 64,
        candidate_id="a" * 64,
        source_policy_id="c" * 64,
        source_start=NOW,
        training_start=NOW + timedelta(days=1),
        split_at=NOW + timedelta(days=8),
        evaluation_end=NOW + timedelta(days=15),
        modeled_cost_contract_id=contract.contract_id,
    )
    evaluation = ContextImpactEvaluation(
        experiment_id="synthetic",
        recorded_at=NOW,
        candidate_id="a" * 64,
        plan_id=plan.plan_id,
        request_id="d" * 64,
        cost_basis="MODELED_NET",
        status="INSUFFICIENT_EVIDENCE",
        reason="old",
    )
    forecast = ContextForecast(
        forecast_spec_id="spec",
        version_id=version.version_id,
        recorded_at=NOW,
        snapshot_id="snapshot",
        candidate_values=(1.0,),
        expected_net_bps=1.0,
    )
    for value in (version, plan, evaluation, forecast):
        payload = value.model_dump()
        assert "fixed_baseline_id" not in payload and "fixed_values" not in payload
        assert content_id(type(value).model_validate(payload)) == content_id(payload)


def test_fixed_lexical_shadow_starts_without_reviews_or_fit_charges(tmp_path):
    from quantbot.learner.context_admission import CanonicalContextAdmission
    from quantbot.learner.context_impact import ContextImpactPlan

    _, store, broker, research, experiments, plan, registry = fixture(tmp_path)
    binding = learning_binding(research)
    contract = ModeledCostContract(
        recorded_at=binding.recorded_at,
        source_policy_refs=(binding.manifest_hash, "explicit-synthetic-crypto-policy"),
        crypto_round_trip_bps=60,
        cost_scope="ROUND_TRIP_FEES_PLUS_SLIPPAGE_V2",
    )
    binding = binding.model_copy(
        update={
            "interpreter_admission": "FIXED_LEXICAL_OUTCOME_SHADOW_V1",
            "modeled_cost_contract_id": contract.contract_id,
            "registry_path": store.database.path,
        }
    )
    with research.connection() as db:
        research._receipt(db, contract)
        research._set(db, BINDING_KEY, binding)
    status = consider_context_learning(store, broker, now=NOW, force=True)
    version = current_version(research)
    assert version and version.mode == "SHADOW_ONLY"
    assert version.candidate_id is None and version.semantic_evaluation_id is None
    assert (
        version.fixed_baseline_id
        and status.candidate_id is None
        and status.independent_reviews == 0
    )
    assert status.interpreter_kind == "UNTRAINED_FIXED_LEXICAL_BASELINE"
    plans = experiments.read_as_of(
        binding.experiment_id, NOW, maximum_records=100, kinds=("ContextImpactPlan",)
    )
    assert len(plans) == 1
    impact = plans[0].decode(ContextImpactPlan)
    assert impact.fixed_baseline_id == version.fixed_baseline_id and impact.candidate_id is None
    assert (
        impact.training_start > NOW
        and impact.evaluation_end == impact.training_start + timedelta(days=14)
    )
    news(research)
    snapshot = capture_research_context(
        research, symbol="AAPL", at=NOW, permissions={"alpaca-news": permissions()}
    )
    original = observation(snapshot.snapshot_id)
    attached = attach_context_interpreter(research, original, now=NOW)
    assert attached == original and attached.context_filter is None
    receipt = load_shadow_consumption(research, observation_id=attached.id)
    assert receipt.fixed_baseline_id == version.fixed_baseline_id and receipt.candidate_id is None
    assert receipt.fixed_feature_values == tuple(value for _, value in receipt.shadow.features)
    assert not receipt.reading_predictions and receipt.candidate_feature_values is None
    assert not load_context_capture(research, snapshot.snapshot_id).snapshot.used_document_ids
    assert (
        CanonicalContextAdmission(
            plan, registry_path=registry, declaration_path=registry, now=plan.evaluation_end
        ).used_calls(binding.experiment_id)
        == 0
    )
    later = consider_context_learning(store, broker, now=NOW + timedelta(hours=1), force=True)
    assert later.interpreter_version_id == version.version_id
    assert (
        len(
            experiments.read_as_of(
                binding.experiment_id,
                NOW + timedelta(hours=1),
                maximum_records=100,
                kinds=("ContextImpactPlan",),
            )
        )
        == 1
    )
    cutoff = consider_context_learning(store, broker, now=impact.evaluation_end, force=True)
    assert cutoff.impact_status == "INSUFFICIENT_EVIDENCE"
    assert cutoff.candidate_id is None and cutoff.independent_reviews == 0
    assert current_version(research) == version
    assert (
        CanonicalContextAdmission(
            plan, registry_path=registry, declaration_path=registry, now=impact.evaluation_end
        ).used_calls(binding.experiment_id)
        == 0
    )
