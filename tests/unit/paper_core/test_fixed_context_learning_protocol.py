"""Real scheduled synthetic core outcomes reach paired research without semantic labels."""

from datetime import timedelta
from decimal import Decimal

import pytest
from tests.unit.learner import test_context_integration as source_fixture
from tests.unit.paper_core.test_external_context import NOW, permissions

from quantbot.learner.context_admission import (
    ContextEvaluationPlan,
    ContextReadingScope,
    register_context_plan,
)
from quantbot.learner.context_impact import CanonicalImpactAdmission, ContextImpactPlan
from quantbot.learner.context_runtime_feedback import ModeledCostContract
from quantbot.learner.five_minute import FEATURE_NAMES
from quantbot.paper_core.context_interpreter import current_version
from quantbot.paper_core.context_learning import (
    BINDING_KEY,
    ContextLearningBinding,
    consider_context_learning,
)
from quantbot.paper_core.contracts import CoreConfig, ModelArtifact, Observation
from quantbot.paper_core.guardian import guardian_cycle
from quantbot.paper_core.outcomes import process_outcomes
from quantbot.paper_core.roles import attach_observed_context
from quantbot.paper_core.runtime import initialize_fixture
from quantbot.paper_core.simulation import SyntheticBroker
from quantbot.paper_core.store import CoreStore
from quantbot.paper_core.trader import trader_cycle
from quantbot.research.manifest import content_id
from quantbot.storage import Database


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_churn", [False, True])
async def test_scheduled_fixed_news_outcomes_fit_twice_without_reviews(
    tmp_path, monkeypatch, legacy_churn
):
    class ExplicitFeeBroker(SyntheticBroker):
        async def submit_order(self, intent):
            order = await super().submit_order(intent)
            self.cash -= Decimal(".25")
            self.fills[-1] = self.fills[-1].model_copy(
                update={
                    "fee": Decimal(".25"),
                    "fee_source": "BROKER_REPORTED",
                    "fee_reference": "EXPLICIT_SYNTHETIC_FEE_FIXTURE_ONLY",
                }
            )
            return order

    research, experiments = source_fixture.stores(tmp_path)
    database = Database.create(tmp_path / "synthetic-core.db")
    store = CoreStore(database, CoreConfig(account_id="synthetic-account", symbol="AAPL"))
    broker = ExplicitFeeBroker(account_id="synthetic-account", now=NOW)
    broker.research_state = research
    initialize_fixture(store, broker, now=NOW, entry_enabled=True)
    rights = {"alpaca-news": permissions()}
    with research.connection() as connection:
        research._set(connection, "context:source-permissions", list(rights.values()))
    start = NOW + timedelta(days=1)
    plan = ContextEvaluationPlan(
        experiment_id="synthetic-context",
        recorded_at=NOW,
        manifest_hash=experiments.state("synthetic-context").manifest_hash,
        source_start=start - timedelta(hours=48),
        training_start=start,
        split_at=start + timedelta(days=7),
        evaluation_end=start + timedelta(days=14),
        reading_scope=ContextReadingScope(source_policy_id=content_id(rights)),
    )
    register_context_plan(experiments, plan, now=NOW)
    costs = ModeledCostContract(
        recorded_at=NOW,
        source_policy_refs=(plan.manifest_hash, "synthetic-total-cost-policy"),
        crypto_round_trip_bps=60,
        cost_scope="ROUND_TRIP_FEES_PLUS_SLIPPAGE_V2",
    )
    binding = ContextLearningBinding(
        experiment_id=plan.experiment_id,
        core_store_identity=database.store_identity,
        experiment_store_path=experiments.path,
        registry_path=database.path,
        declaration_path=database.path,
        manifest_hash=plan.manifest_hash,
        plan_id=plan.plan_id,
        recorded_at=NOW,
        capture_rowid=0,
        decision_rowid=0,
        outcome_rowid=0,
        interpreter_admission="FIXED_LEXICAL_OUTCOME_SHADOW_V1",
        modeled_cost_contract_id=costs.contract_id,
    )
    with research.connection() as connection:
        research._receipt(connection, costs)
        research._set(connection, BINDING_KEY, binding)
    try:
        initial = consider_context_learning(store, broker, now=NOW, force=True)
        version = current_version(research)
        assert version.fixed_baseline_id and initial.candidate_id is None
        impact = next(
            e.decode(ContextImpactPlan)
            for e in experiments.read_as_of(
                plan.experiment_id, NOW, maximum_records=100, kinds=("ContextImpactPlan",)
            )
        )
        admission = CanonicalImpactAdmission(
            impact,
            registry_path=database.path,
            declaration_path=database.path,
            now=impact.evaluation_end,
        )
        assert admission.used_calls() == 0
        from sqlalchemy import select

        import quantbot.paper_core.outcomes as resolving
        from quantbot.paper_core.contracts import RoundTrip
        from quantbot.paper_core.store import table

        current_proof_policy = resolving._historical_quantity_proof_allowed

        def old_proof_policy(store, session, proof, lot_clients):
            revoked = table("core_cash_authority_revocations")
            return (
                session.execute(
                    select(revoked.c.snapshot_id).where(
                        revoked.c.snapshot_id == "core-certified-" + proof.id
                    )
                ).first()
                is None
            )

        if legacy_churn:
            monkeypatch.setattr(resolving, "_historical_quantity_proof_allowed", old_proof_policy)
        retained_current = None
        for index in range(12):
            if index == 2:
                monkeypatch.setattr(
                    resolving, "_historical_quantity_proof_allowed", current_proof_policy
                )
            broker.now = start + timedelta(days=index * 0.75 if index < 8 else 9 + index - 8)
            monkeypatch.setattr(source_fixture, "NOW", broker.now - timedelta(days=index))
            source_fixture.news(research, index)
            await guardian_cycle(store, broker, now=broker.now)
            observed = Observation(
                symbol="AAPL",
                recorded_at_utc=broker.now,
                source_start=broker.now - timedelta(minutes=5),
                source_end=broker.now,
                available_at=broker.now,
                price=Decimal(100),
                raw_data_digest=f"synthetic:{index}",
                features=tuple((name, 0.01 * (index + 1)) for name in FEATURE_NAMES),
                dependencies=(),
                dependency_available_at=(),
                sequence=index + 1,
                session_close=broker.now + timedelta(hours=6),
            )
            observed = attach_observed_context(research, observed, at=broker.now, core_store=store)
            decision = await trader_cycle(
                store, broker, observed, ModelArtifact(), store.latest_proof(), now=broker.now
            )
            assert decision.action == "BUY", decision.gates
            assert len(broker.posts) == index * 2 + 1
            broker.now += timedelta(minutes=30)
            await guardian_cycle(store, broker, now=broker.now)
            process_outcomes(store, cutoff=broker.now)
            if index == 1:
                with database.transaction() as session:
                    versions = [
                        RoundTrip.model_validate_json(r)
                        for r in session.execute(
                            select(table("core_outcome_revisions").c.payload)
                        ).scalars()
                    ]
                first = min(versions, key=lambda r: r.source_start)
                retained_current = max(
                    (r for r in versions if r.lot_id == first.lot_id), key=lambda r: r.revision
                )
                assert retained_current.revision == (2 if legacy_churn else 1)
            considered = consider_context_learning(store, broker, now=broker.now, force=True)
            assert considered.independent_reviews == 0 and considered.candidate_id is None
            assert admission.used_calls() == 0
        import quantbot.learner.context_feedback as fitting

        original_fit, calls = fitting.fit_estimator, []

        def charged_fit(*args, **kwargs):
            assert admission.used_calls() == 2
            calls.append(args[2])
            return original_fit(*args, **kwargs)

        monkeypatch.setattr(fitting, "fit_estimator", charged_fit)
        result = consider_context_learning(store, broker, now=impact.evaluation_end, force=True)
        assert result.impact_status == "EVALUATED", result.gate_counts
        assert result.candidate_id is None and result.independent_reviews == 0
        assert len(calls) == 2 and admission.used_calls() == 2
        again = consider_context_learning(
            store, broker, now=impact.evaluation_end + timedelta(hours=1), force=True
        )
        assert again.impact_status == "EVALUATED" and len(calls) == 2
        assert current_version(research) == version and version.mode == "SHADOW_ONLY"
        with database.transaction() as session:
            versions = [
                RoundTrip.model_validate_json(r)
                for r in session.execute(
                    select(table("core_outcome_revisions").c.payload)
                ).scalars()
            ]
        assert len(versions) == (13 if legacy_churn else 12)
        assert (
            max(
                (r for r in versions if r.lot_id == retained_current.lot_id),
                key=lambda r: r.revision,
            )
            == retained_current
        )
        assert not experiments.read_as_of(
            plan.experiment_id,
            impact.evaluation_end,
            maximum_records=100,
            kinds=("ReadingReview", "InterpreterCandidate"),
        )
    finally:
        database.close()
