"""Prospective context versions and an explicitly fenced, veto-only trading overlay."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Literal, TypeVar

from pydantic import Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantbot.learner.context_admission import ContextEvaluationPlan
from quantbot.learner.context_feedback import candidate_polarity
from quantbot.learner.context_impact import (
    ContextImpactComparison,
    ContextImpactEvaluation,
    ContextImpactPlan,
    load_retained_impact_comparison,
    register_context_impact_plan,
)
from quantbot.learner.context_integration import (
    ContextEvaluation,
    ContextInvalidationRecord,
    load_retained_candidate,
)
from quantbot.learner.context_runtime_feedback import (
    ContextShadowConsumption,
    ModeledCostContract,
    _source_gate,
    load_shadow_consumption,
    retain_shadow_consumption,
)
from quantbot.learner.estimators import predict
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.models import LearnerModel
from quantbot.paper_core.context_integration import (
    load_context_capture,
    load_source_permissions,
    retained_context_documents,
)
from quantbot.paper_core.context_learning import BINDING_KEY, ContextLearningBinding
from quantbot.paper_core.contracts import Observation, ObservationContextFilterInput
from quantbot.paper_core.discovery import DiscoveryStore
from quantbot.paper_core.external_context import (
    CONTEXT_FEATURE_NAMES,
    ContextSnapshot,
    aware,
    build_context_snapshot,
)
from quantbot.paper_core.store import CoreStore
from quantbot.research.manifest import content_id

POINTER_KEY = "context:interpreter-current"
T = TypeVar("T", bound=LearnerModel)


class ContextInterpreterVersion(LearnerModel):
    contract: Literal["CONTEXT_INTERPRETER_VERSION_V1"] = "CONTEXT_INTERPRETER_VERSION_V1"
    binding_id: str
    recorded_at: datetime
    mode: Literal["SHADOW_ONLY", "TRADING_FILTER", "DISABLED"]
    candidate_id: str
    semantic_evaluation_id: str
    impact_plan_id: str
    source_policy_id: str
    previous_version_id: str | None = None
    approval_ref: str | None = None
    impact_evaluation_id: str | None = None
    impact_comparison_id: str | None = None
    commission_id: str | None = None
    price_activation_id: str | None = None
    scope: Literal["OPTIONAL_ELIGIBLE_NEWS_VETO_ONLY_V1"] = "OPTIONAL_ELIGIBLE_NEWS_VETO_ONLY_V1"

    @property
    def version_id(self) -> str:
        return content_id(self)


class ContextForecast(LearnerModel):
    forecast_spec_id: str
    version_id: str
    recorded_at: datetime
    snapshot_id: str
    candidate_values: tuple[float, ...]
    expected_net_bps: float = Field(allow_inf_nan=False)

    @property
    def forecast_id(self) -> str:
        return content_id(self)


def _receipt(research: DiscoveryStore, identity: str, model: type[T]) -> T:
    with research.connection() as db:
        row = db.execute("SELECT payload FROM receipts WHERE id=?", (identity,)).fetchone()
    if row is None:
        raise ValueError("CONTEXT_ARTIFACT_UNAVAILABLE")
    value = model.model_validate_json(row[0])
    if content_id(value) != identity:
        raise ValueError("CONTEXT_ARTIFACT_HASH_MISMATCH")
    return value


def learning_binding(research: DiscoveryStore) -> ContextLearningBinding | None:
    with research.connection() as db:
        raw = research._get(db, BINDING_KEY)
    return ContextLearningBinding.model_validate(raw) if raw is not None else None


def current_version(research: DiscoveryStore) -> ContextInterpreterVersion | None:
    with research.connection() as db:
        identity = research._get(db, POINTER_KEY)
        history = research._get(db, "context:interpreter-history", [])
    if identity is None:
        if history:
            raise ValueError("CONTEXT_INTERPRETER_POINTER_MISSING")
        return None
    if not history or history[-1] != identity or len(set(history)) != len(history):
        raise ValueError("CONTEXT_INTERPRETER_POINTER_INVALID")
    return _receipt(research, identity, ContextInterpreterVersion)


def _publish(
    research: DiscoveryStore, version: ContextInterpreterVersion, expected: str | None
) -> None:
    with research.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if research._get(db, POINTER_KEY) != expected:
            raise ValueError("CONTEXT_INTERPRETER_CAS_CONFLICT")
        history = research._get(db, "context:interpreter-history", [])
        if (history[-1] if history else None) != expected:
            raise ValueError("CONTEXT_INTERPRETER_HISTORY_INVALID")
        research._receipt(db, version)
        research._set(db, "context:interpreter-history", [*history, version.version_id])
        research._set(db, POINTER_KEY, version.version_id)


def modeled_contract(
    research: DiscoveryStore, binding: ContextLearningBinding
) -> ModeledCostContract | None:
    if binding.modeled_cost_contract_id is None:
        return None
    contract = _receipt(research, binding.modeled_cost_contract_id, ModeledCostContract)
    if (
        contract.recorded_at > binding.recorded_at
        or binding.manifest_hash not in contract.source_policy_refs
    ):
        raise ValueError("CONTEXT_MODELED_COST_BINDING_INVALID")
    return contract


def admit_shadow(
    research: DiscoveryStore,
    experiments: ExperimentStore,
    binding: ContextLearningBinding,
    evaluation: ContextEvaluation,
    *,
    now: datetime,
) -> ContextInterpreterVersion | None:
    """Initial commission permits one automatic shadow version, never trading promotion."""
    current = current_version(research)
    if current is not None or binding.interpreter_admission is None:
        return current
    if evaluation.status != "EVALUATED" or evaluation.candidate_id is None:
        return None
    events = experiments.read_as_of(
        binding.experiment_id, now, maximum_records=4000, kinds=("ContextEvaluation",)
    )
    if not any(e.decode(ContextEvaluation) == evaluation for e in events):
        raise ValueError("CONTEXT_SEMANTIC_EVALUATION_NOT_RETAINED")
    candidate = load_retained_candidate(
        experiments,
        experiment_id=binding.experiment_id,
        candidate_id=evaluation.candidate_id,
        at=now,
    )
    if candidate.heldout_mse >= candidate.baseline_heldout_mse:
        return None
    contract = modeled_contract(research, binding)
    if contract is None:
        raise ValueError("CONTEXT_MODELED_COST_CONTRACT_REQUIRED")
    start = aware(now).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    policy_id = content_id(load_source_permissions(research))
    plan = ContextImpactPlan(
        experiment_id=binding.experiment_id,
        recorded_at=now,
        manifest_hash=binding.manifest_hash,
        candidate_id=candidate.candidate_id,
        source_policy_id=policy_id,
        source_start=start - timedelta(hours=48),
        training_start=start,
        split_at=start + timedelta(days=7),
        evaluation_end=start + timedelta(days=14),
        modeled_cost_contract_id=contract.contract_id,
    )
    # A crash after registration must reuse the immutable registered plan.
    prior = experiments.read_as_of(
        binding.experiment_id, now, maximum_records=4000, kinds=("ContextImpactPlan",)
    )
    plans = [
        e.decode(ContextImpactPlan)
        for e in prior
        if e.decode(ContextImpactPlan).candidate_id == candidate.candidate_id
    ]
    if plans:
        if len(plans) != 1:
            raise ValueError("CONTEXT_IMPACT_PLAN_AMBIGUOUS")
        plan = plans[0]
    else:
        register_context_impact_plan(experiments, plan, now=now)
    version = ContextInterpreterVersion(
        binding_id=content_id(binding),
        recorded_at=now,
        mode="SHADOW_ONLY",
        candidate_id=candidate.candidate_id,
        semantic_evaluation_id=content_id(evaluation),
        impact_plan_id=plan.plan_id,
        source_policy_id=policy_id,
    )
    _publish(research, version, None)
    return version


def _validated(
    research: DiscoveryStore,
    binding: ContextLearningBinding,
    version: ContextInterpreterVersion,
    *,
    now: datetime,
    core_store: CoreStore | None = None,
    session: Session | None = None,
) -> tuple[ExperimentStore, ContextImpactComparison | None]:
    if (
        version.binding_id != content_id(binding)
        or version.recorded_at > now
        or version.source_policy_id != content_id(load_source_permissions(research))
        or not binding.experiment_store_path.is_file()
    ):
        raise ValueError("CONTEXT_INTERPRETER_BINDING_INVALID")
    experiments = ExperimentStore(binding.experiment_store_path)
    if experiments.state(binding.experiment_id).manifest_hash != binding.manifest_hash:
        raise ValueError("CONTEXT_MANIFEST_BINDING_INVALID")
    load_retained_candidate(
        experiments, experiment_id=binding.experiment_id, candidate_id=version.candidate_id, at=now
    )
    events = experiments.read_as_of(
        binding.experiment_id,
        now,
        maximum_records=4000,
        kinds=("ContextEvaluation", "ContextImpactPlan"),
    )
    evaluations = [
        e.decode(ContextEvaluation)
        for e in events
        if e.kind == "ContextEvaluation"
        and content_id(e.decode(ContextEvaluation)) == version.semantic_evaluation_id
    ]
    plans = [
        e.decode(ContextImpactPlan)
        for e in events
        if e.kind == "ContextImpactPlan"
        and e.decode(ContextImpactPlan).plan_id == version.impact_plan_id
    ]
    if (
        len(evaluations) != 1
        or evaluations[0].candidate_id != version.candidate_id
        or evaluations[0].evaluation_plan_id != binding.plan_id
        or evaluations[0].status != "EVALUATED"
        or len(plans) != 1
        or plans[0].candidate_id != version.candidate_id
        or plans[0].source_policy_id != version.source_policy_id
    ):
        raise ValueError("CONTEXT_VERSION_LINEAGE_INVALID")
    comparison = None
    if version.mode == "TRADING_FILTER":
        if version.impact_comparison_id is None:
            raise ValueError("CONTEXT_TRADING_ARTIFACT_INCOMPLETE")
        comparison = load_retained_impact_comparison(
            experiments,
            experiment_id=binding.experiment_id,
            comparison_id=version.impact_comparison_id,
            at=now,
            core_store=core_store,
            session=session,
        )
        if (
            comparison.candidate_id != version.candidate_id
            or comparison.plan_id != version.impact_plan_id
        ):
            raise ValueError("CONTEXT_TRADING_LINEAGE_INVALID")
        if core_store is not None and (
            binding.core_store_identity != core_store.database.store_identity
            or binding.registry_path.resolve() != core_store.database.path.resolve()
            or core_store.config.evidence_lane == "BROKER_PAPER"
            and comparison.evidence_lane != "BROKER_PAPER"
        ):
            raise ValueError("CONTEXT_CANONICAL_TRADING_STORE_MISMATCH")
    return experiments, comparison


def _binding_experiments(
    research: DiscoveryStore, binding: ContextLearningBinding, *, now: datetime
) -> ExperimentStore:
    if not binding.experiment_store_path.is_file() or binding.recorded_at > now:
        raise ValueError("CONTEXT_COMMISSIONED_STORE_UNAVAILABLE")
    experiments = ExperimentStore(binding.experiment_store_path)
    if experiments.state(binding.experiment_id).manifest_hash != binding.manifest_hash:
        raise ValueError("CONTEXT_MANIFEST_BINDING_INVALID")
    events = experiments.read_as_of(
        binding.experiment_id, now, maximum_records=4000, kinds=("ContextEvaluationPlan",)
    )
    plans = [e.decode(ContextEvaluationPlan) for e in events]
    if (
        len(plans) != 1
        or plans[0].plan_id != binding.plan_id
        or plans[0].reading_scope is None
        or plans[0].reading_scope.source_policy_id != content_id(load_source_permissions(research))
    ):
        raise ValueError("CONTEXT_COMMISSIONED_READING_SCOPE_INVALID")
    return experiments


def _vectors(
    experiments: ExperimentStore,
    binding: ContextLearningBinding,
    candidate_id: str,
    shadow: ContextSnapshot,
    *,
    now: datetime,
) -> tuple[tuple[float, ...], tuple[tuple[str, float], ...]]:
    candidate = load_retained_candidate(
        experiments, experiment_id=binding.experiment_id, candidate_id=candidate_id, at=now
    )
    events = experiments.read_as_of(
        binding.experiment_id, now, maximum_records=4000, kinds=("ContextInvalidationRecord",)
    )
    invalidations = tuple(e.decode(ContextInvalidationRecord).invalidation for e in events)
    readings = tuple(r for r in shadow.readings if r.document_id in shadow.used_document_ids)
    predictions = tuple(
        (r.reading_id, candidate_polarity(candidate, r, at=now, invalidations=invalidations))
        for r in readings
    )
    values = tuple(v for _, v in predictions)
    positive, negative = sum(v > 0 for v in values), sum(v < 0 for v in values)
    features = (
        float(len(values)),
        float(positive),
        float(negative),
        sum(values) / len(values),
        float(bool(positive and negative)),
        shadow.features[-1][1],
    )
    return features, predictions


def forecast_spec_id(observation: Observation) -> str:
    return content_id(observation.model_dump(exclude={"context_filter"}))


def attach_context_interpreter(
    research: DiscoveryStore,
    observation: Observation,
    *,
    now: datetime,
    core_store: CoreStore | None = None,
) -> Observation:
    """Prepare genuine shadow inputs before decisions; preserve original observed snapshot."""
    try:
        binding = learning_binding(research)
        version = current_version(research)
        if binding is None:
            if version is not None:
                raise ValueError("CONTEXT_LEARNING_BINDING_MISSING")
            return observation
        if version is not None and version.mode == "DISABLED":
            return observation
        experiments = _binding_experiments(research, binding, now=now)
        comparison = None
        if version is not None:
            if (
                version.mode == "TRADING_FILTER"
                and observation.evidence_lane != "SYNTHETIC_TEST"
                and core_store is None
            ):
                raise ValueError("CURRENT_OUTCOME_REVALIDATION_REQUIRED")
            experiments, comparison = _validated(
                research, binding, version, now=now, core_store=core_store
            )
        if observation.context_snapshot_id is None:
            return observation
        capture = load_context_capture(research, observation.context_snapshot_id)
        invalidations = experiments.read_as_of(
            binding.experiment_id, now, maximum_records=4000, kinds=("ContextInvalidationRecord",)
        )
        revoked = {
            e.decode(ContextInvalidationRecord).invalidation.reference_id for e in invalidations
        }
        reason = _source_gate(research, capture, load_source_permissions(research), revoked, now)
        if reason:
            raise ValueError(reason)
        docs = retained_context_documents(research, capture)
        unused = build_context_snapshot(
            docs, symbol=observation.symbol, at=now, coverage=capture.snapshot.coverage
        )
        if not unused.eligible_document_ids:
            return observation
        shadow = build_context_snapshot(
            docs,
            symbol=observation.symbol,
            at=now,
            coverage=capture.snapshot.coverage,
            used_document_ids=unused.eligible_document_ids,
        )
        values = None
        predictions: tuple[tuple[str, float], ...] = ()
        if version is not None:
            assert experiments is not None
            values, predictions = _vectors(
                experiments, binding, version.candidate_id, shadow, now=now
            )
        if version is not None and version.mode == "TRADING_FILTER":
            if comparison is None or values is None or version.impact_comparison_id is None:
                raise ValueError("CONTEXT_TRADING_ARTIFACT_INCOMPLETE")
            inputs = dict(observation.features) | dict(
                zip(CONTEXT_FEATURE_NAMES, values, strict=True)
            )
            estimator = comparison.comparison.context_estimator
            prediction = predict(estimator, (tuple(inputs[n] for n in estimator.feature_names),))[0]
            forecast = ContextForecast(
                forecast_spec_id=forecast_spec_id(observation),
                version_id=version.version_id,
                recorded_at=now,
                snapshot_id=shadow.snapshot_id,
                candidate_values=values,
                expected_net_bps=prediction,
            )
            with research.connection() as db:
                research._receipt(db, forecast)
            observation = observation.model_copy(
                update={
                    "context_filter": ObservationContextFilterInput(
                        forecast_spec_id=forecast.forecast_spec_id,
                        forecast_id=forecast.forecast_id,
                        version_id=version.version_id,
                        expected_net_bps=prediction,
                    )
                }
            )
        existing = load_shadow_consumption(research, observation_id=observation.id)
        if existing is None:
            receipt = ContextShadowConsumption(
                observation_id=observation.id,
                original_snapshot_id=observation.context_snapshot_id,
                consumed_at=now,
                recorded_at=now,
                shadow=shadow,
                candidate_id=version.candidate_id if version else None,
                candidate_feature_values=values,
                reading_predictions=predictions,
            )

            def validate_candidate(receipt: ContextShadowConsumption) -> bool:
                if experiments is None or receipt.candidate_id is None:
                    return False
                return (receipt.candidate_feature_values, receipt.reading_predictions) == _vectors(
                    experiments, binding, receipt.candidate_id, receipt.shadow, now=now
                )

            retain_shadow_consumption(
                research,
                receipt,
                now=now,
                candidate_validator=validate_candidate,
            )
        return observation
    except (ValueError, KeyError, OSError, sqlite3.Error) as error:
        # Missing or corrupt active authority is a visible refusal, never a baseline bypass.
        return observation.model_copy(
            update={
                "context_filter": ObservationContextFilterInput(
                    forecast_spec_id=forecast_spec_id(observation),
                    forecast_id="UNAVAILABLE",
                    version_id="UNAVAILABLE",
                    objection="CONTEXT_INTERPRETER_UNAVAILABLE:" + str(error),
                )
            }
        )


def filter_objections(
    research: DiscoveryStore | None,
    observation: Observation,
    *,
    now: datetime,
    core_store: CoreStore | None = None,
    session: Session | None = None,
) -> tuple[str, ...]:
    overlay = observation.context_filter
    if research is None:
        return ("CONTEXT_FILTER_RESEARCH_UNAVAILABLE",) if overlay is not None else ()
    try:
        binding = learning_binding(research)
        version = current_version(research)
        if version is None or version.mode != "TRADING_FILTER":
            return ("CONTEXT_FILTER_VERSION_CHANGED",) if overlay is not None else ()
        if binding is None:
            raise ValueError("CONTEXT_LEARNING_BINDING_MISSING")
        if observation.evidence_lane != "SYNTHETIC_TEST" and core_store is None:
            raise ValueError("CURRENT_OUTCOME_REVALIDATION_REQUIRED")
        experiments, comparison = _validated(
            research, binding, version, now=now, core_store=core_store, session=session
        )
        if comparison is None or version.impact_comparison_id is None:
            raise ValueError("CONTEXT_TRADING_ARTIFACT_INCOMPLETE")
        if observation.context_snapshot_id is None:
            if overlay is not None:
                raise ValueError("CONTEXT_FILTER_SNAPSHOT_MISSING")
            return ()
        capture = load_context_capture(research, observation.context_snapshot_id)
        events = experiments.read_as_of(
            binding.experiment_id, now, maximum_records=4000, kinds=("ContextInvalidationRecord",)
        )
        revoked = {e.decode(ContextInvalidationRecord).invalidation.reference_id for e in events}
        reason = _source_gate(research, capture, load_source_permissions(research), revoked, now)
        if reason:
            raise ValueError(reason)
        if overlay is None:
            available = build_context_snapshot(
                retained_context_documents(research, capture),
                symbol=observation.symbol,
                at=now,
                coverage=capture.snapshot.coverage,
            )
            if available.eligible_document_ids:
                raise ValueError("CONTEXT_FILTER_FORECAST_MISSING")
            return ()
        if overlay.objection or overlay.version_id != version.version_id:
            raise ValueError("CONTEXT_FILTER_VERSION_OR_INPUT_INVALID")
        forecast = _receipt(research, overlay.forecast_id, ContextForecast)
        receipt = load_shadow_consumption(research, observation_id=observation.id)
        if (
            forecast.version_id != version.version_id
            or forecast.forecast_spec_id != forecast_spec_id(observation)
            or overlay.forecast_spec_id != forecast.forecast_spec_id
            or not 0 <= (now - forecast.recorded_at).total_seconds() <= 120
            or receipt is None
            or receipt.shadow.snapshot_id != forecast.snapshot_id
            or receipt.candidate_id != version.candidate_id
        ):
            raise ValueError("CONTEXT_FILTER_FORECAST_INVALID_OR_STALE")
        values, predictions = _vectors(
            experiments, binding, version.candidate_id, receipt.shadow, now=now
        )
        inputs = dict(observation.features) | dict(zip(CONTEXT_FEATURE_NAMES, values, strict=True))
        estimator = comparison.comparison.context_estimator
        expected = predict(estimator, (tuple(inputs[n] for n in estimator.feature_names),))[0]
        if (
            values != forecast.candidate_values
            or predictions != receipt.reading_predictions
            or expected != forecast.expected_net_bps
            or expected != overlay.expected_net_bps
        ):
            raise ValueError("CONTEXT_FILTER_PREDICTION_INVALID")
        return ("CONTEXT_PREDICTED_NET_NONPOSITIVE",) if expected <= 0 else ()
    except (ValueError, KeyError, OSError, sqlite3.Error) as error:
        return ("CONTEXT_FILTER_UNVERIFIED:" + str(error),)


def change_interpreter_mode(
    store: CoreStore,
    research: DiscoveryStore,
    *,
    expected_version_id: str,
    candidate_id: str,
    semantic_evaluation_id: str,
    commission_id: str,
    price_activation_id: str,
    approval_ref: str,
    now: datetime,
    mode: Literal["TRADING_FILTER", "DISABLED"],
    impact_evaluation_id: str | None = None,
) -> ContextInterpreterVersion:
    """Explicit operator API. Rollback disables the overlay; neither action clears the fence."""
    from quantbot.paper_core.activation import active_in_session
    from quantbot.paper_core.authority import commission_in_session, current_runtime_event

    if not approval_ref.strip():
        raise ValueError("CONTEXT_MODE_APPROVAL_REQUIRED")
    binding = learning_binding(research)
    if (
        binding is None
        or binding.registry_path.resolve() != store.database.path.resolve()
        or binding.core_store_identity != store.database.store_identity
    ):
        raise ValueError("CONTEXT_CANONICAL_LEDGER_REQUIRED")
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        active = active_in_session(store, session)
        if (
            current_runtime_event(session).action != "FENCE_ENTRY"
            or commission_in_session(store, session).id != commission_id
            or active is None
            or active[1].id != price_activation_id
        ):
            raise ValueError("CONTEXT_MODE_REQUIRES_EXACT_FENCED_COMMISSION")
        current = current_version(research)
        if (
            current is None
            or current.version_id != expected_version_id
            or current.candidate_id != candidate_id
            or current.semantic_evaluation_id != semantic_evaluation_id
        ):
            raise ValueError("CONTEXT_MODE_EXPECTED_VERSION_MISMATCH")
        updates: dict[str, Any] = dict(
            mode=mode,
            recorded_at=now,
            previous_version_id=current.version_id,
            approval_ref=approval_ref,
            commission_id=commission_id,
            price_activation_id=price_activation_id,
        )
        if mode == "TRADING_FILTER":
            experiments, _ = _validated(
                research, binding, current, now=now, core_store=store, session=session
            )
            events = experiments.read_as_of(
                binding.experiment_id, now, maximum_records=4000, kinds=("ContextImpactEvaluation",)
            )
            evaluations = [
                e.decode(ContextImpactEvaluation)
                for e in events
                if content_id(e.decode(ContextImpactEvaluation)) == impact_evaluation_id
            ]
            if len(evaluations) != 1:
                raise ValueError("CONTEXT_EXACT_IMPACT_EVALUATION_REQUIRED")
            evaluation = evaluations[0]
            if (
                evaluation.status != "EVALUATED"
                or evaluation.candidate_id != current.candidate_id
                or evaluation.plan_id != current.impact_plan_id
                or evaluation.selected_heldout_count < 4
                or evaluation.selected_heldout_mean_net_bps is None
                or evaluation.selected_heldout_mean_net_bps <= 0
                or evaluation.comparison_id is None
            ):
                raise ValueError("CONTEXT_COST_AWARE_IMPROVEMENT_REQUIRED")
            comparison = load_retained_impact_comparison(
                experiments,
                experiment_id=binding.experiment_id,
                comparison_id=evaluation.comparison_id,
                at=now,
                core_store=store,
                session=session,
            )
            plans = experiments.read_as_of(
                binding.experiment_id, now, maximum_records=4000, kinds=("ContextImpactPlan",)
            )
            plan = next(
                (
                    e.decode(ContextImpactPlan)
                    for e in plans
                    if e.decode(ContextImpactPlan).plan_id == current.impact_plan_id
                ),
                None,
            )
            from quantbot.research.intraday_accounting import verified_intraday_attempt_ids

            prefix = (
                "intraday:context-impact-"
                + content_id(binding.experiment_id)[:16]
                + ":"
                + current.impact_plan_id[:16]
                + ":"
                + evaluation.request_id
            )
            expected_trials = (prefix + ":p", prefix + ":c")
            if (
                plan is None
                or not plan.evaluation_end
                <= comparison.recorded_at
                <= evaluation.recorded_at
                <= now
                or comparison.request_id != evaluation.request_id
                or evaluation.cost_basis != comparison.cost_basis
                or evaluation.cost_basis != plan.cost_basis
                or comparison.trial_ids != expected_trials
                or not set(expected_trials) <= verified_intraday_attempt_ids(session)
                or plan.modeled_cost_contract_id != binding.modeled_cost_contract_id
            ):
                raise ValueError("CONTEXT_CANONICAL_IMPACT_AUTHORITY_REQUIRED")
            if (
                comparison.candidate_id != current.candidate_id
                or comparison.plan_id != current.impact_plan_id
                or comparison.comparison.price_plus_context_mse
                >= comparison.comparison.price_only_mse
            ):
                raise ValueError("CONTEXT_PAIRED_IMPROVEMENT_REQUIRED")
            updates.update(
                impact_evaluation_id=impact_evaluation_id,
                impact_comparison_id=evaluation.comparison_id,
            )
        version = ContextInterpreterVersion.model_validate(current.model_dump() | updates)
        _publish(research, version, current.version_id)
        return version
