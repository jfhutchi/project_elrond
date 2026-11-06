"""Predeclared, permanently charged paired context research; never execution authority."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self

from pydantic import Field, model_validator
from sqlalchemy import text

from quantbot.learner.context_admission import _session
from quantbot.learner.context_feedback import (
    DownstreamComparison,
    DownstreamExample,
    compare_context_to_price_only,
)
from quantbot.learner.context_integration import ContextInvalidationRecord, load_retained_candidate
from quantbot.learner.estimators import predict
from quantbot.learner.experiment_contract import Digest
from quantbot.learner.experiment_research import experiment_trial_prefixes
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.five_minute import FEATURE_NAMES
from quantbot.learner.models import LearnerModel
from quantbot.learner.service import guard_range
from quantbot.paper_core.context_integration import (
    load_context_capture,
    load_source_permissions,
    retained_context_documents,
)
from quantbot.paper_core.contracts import Observation
from quantbot.paper_core.discovery import DiscoveryStore
from quantbot.paper_core.external_context import ContextDocument, aware
from quantbot.research.intraday_accounting import (
    record_intraday_attempt,
    verified_intraday_attempt_ids,
)
from quantbot.research.manifest import content_id

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from quantbot.learner.context_runtime_feedback import ContextOutcomeFeedback
    from quantbot.paper_core.store import CoreStore


class ContextImpactPlan(LearnerModel):
    experiment_id: str
    recorded_at: datetime
    manifest_hash: Digest
    candidate_id: Digest
    source_policy_id: Digest
    source_start: datetime
    training_start: datetime
    split_at: datetime
    evaluation_end: datetime
    contract: Literal["PREDECLARED_CONTEXT_IMPACT_V1"] = "PREDECLARED_CONTEXT_IMPACT_V1"
    target: Literal["OWNED_COST_LABELED_RETURN_BPS"] = "OWNED_COST_LABELED_RETURN_BPS"
    sampling_policy: Literal["FIRST100_PER_SPLIT_SIDE_BY_DECISION_TIME_ID_V1"] = (
        "FIRST100_PER_SPLIT_SIDE_BY_DECISION_TIME_ID_V1"
    )
    source_cluster_policy: Literal["EXCLUDE_HELDOUT_TRAINING_SOURCE_OVERLAP_NO_REFILL_V1"] = (
        "EXCLUDE_HELDOUT_TRAINING_SOURCE_OVERLAP_NO_REFILL_V1"
    )
    cost_basis: Literal["MODELED_NET", "ACTUAL_NET"] = "MODELED_NET"
    modeled_cost_contract_id: Digest | None = None
    stock_round_trip_bps: Literal[10] = 10
    crypto_round_trip_bps: Literal[50] = 50
    cost_assumption_scope: Literal["RESEARCH_ESTIMATE_NOT_BROKER_FEES"] = (
        "RESEARCH_ESTIMATE_NOT_BROKER_FEES"
    )
    grants_activation: Literal[False] = False

    @model_validator(mode="after")
    def predeclared(self) -> Self:
        if self.cost_basis == "MODELED_NET" and self.modeled_cost_contract_id is None:
            raise ValueError("EXACT_PREDECLARED_MODELED_COST_CONTRACT_REQUIRED")
        if self.cost_basis == "ACTUAL_NET" and self.modeled_cost_contract_id is not None:
            raise ValueError("ACTUAL_NET_CANNOT_CARRY_MODELED_COST_CONTRACT")
        for at in (
            self.recorded_at,
            self.source_start,
            self.training_start,
            self.split_at,
            self.evaluation_end,
        ):
            aware(at)
        if not (
            self.recorded_at <= self.training_start < self.split_at < self.evaluation_end
            and self.training_start - timedelta(hours=48)
            <= self.source_start
            <= self.training_start
        ):
            raise ValueError("PREDECLARED_PROSPECTIVE_IMPACT_WINDOWS_REQUIRED")
        return self

    @property
    def plan_id(self) -> str:
        return content_id(self)


class ContextImpactComparison(LearnerModel):
    experiment_id: str
    recorded_at: datetime
    candidate_id: Digest
    plan_id: Digest
    request_id: Digest
    comparison: DownstreamComparison
    cost_basis: Literal["MODELED_NET", "ACTUAL_NET"]
    cost_evidence_ids: tuple[str, ...]
    trial_ids: tuple[str, str]
    feedback_ids: tuple[str, ...] = ()
    core_store_identity: str = ""
    evidence_lane: Literal["SYNTHETIC_TEST", "LOCAL_SIM_REAL_DATA", "BROKER_PAPER"] = "BROKER_PAPER"
    grants_activation: Literal[False] = False

    @property
    def comparison_id(self) -> str:
        return content_id(self)


def select_impact_feedback(
    rows: Sequence[ContextOutcomeFeedback],
    plan: ContextImpactPlan,
) -> tuple[ContextOutcomeFeedback, ...]:
    """Predeclared chronology-only sample; outcome values never affect inclusion."""
    if len(rows) > 4000:
        raise ValueError("IMPACT_FEEDBACK_HARD_BOUND_EXCEEDED")
    ordered = sorted(rows, key=lambda row: (row.decision_at, row.decision_id))
    train = [
        row
        for row in ordered
        if (
            plan.training_start <= row.decision_at < plan.split_at
            and row.label_available_at < plan.split_at
        )
    ][:100]
    heldout = [
        row
        for row in ordered
        if (
            plan.split_at <= row.decision_at <= plan.evaluation_end
            and row.label_available_at <= plan.evaluation_end
        )
    ][:100]
    return tuple((*train, *heldout))


def purge_heldout_source_overlap(
    rows: Sequence[DownstreamExample],
    documents: Mapping[str, ContextDocument],
    *,
    split_at: datetime,
) -> tuple[DownstreamExample, ...]:
    """Fixed no-refill source provenance exclusion, independent of predictions or outcomes."""
    training_clusters = {
        documents[ref].source_id
        for row in rows
        if row.decision_at < split_at
        for ref in row.context.used_document_ids
    }
    return tuple(
        row
        for row in rows
        if row.decision_at < split_at
        or not (
            {documents[ref].source_id for ref in row.context.used_document_ids} & training_clusters
        )
    )


class ContextImpactEvaluation(LearnerModel):
    experiment_id: str
    recorded_at: datetime
    candidate_id: Digest
    plan_id: Digest
    request_id: Digest
    cost_basis: Literal["MODELED_NET", "ACTUAL_NET"]
    status: Literal["EVALUATED", "INSUFFICIENT_EVIDENCE"]
    reason: str
    comparison_id: Digest | None = None
    selected_heldout_count: int = Field(default=0, ge=0)
    selected_heldout_mean_net_bps: float | None = None
    eligible_examples: int = Field(default=0, ge=0)
    grants_activation: Literal[False] = False


def register_context_impact_plan(
    store: ExperimentStore,
    plan: ContextImpactPlan,
    *,
    now: datetime,
) -> bool:
    plan = ContextImpactPlan.model_validate(plan.model_dump())
    now = aware(now)
    if not plan.recorded_at <= now <= plan.training_start:
        raise ValueError("IMPACT_PLAN_MUST_PRECEDE_TRAINING")
    if store.state(plan.experiment_id).manifest_hash != plan.manifest_hash:
        raise ValueError("EXACT_PARENT_MANIFEST_REQUIRED")
    candidate = load_retained_candidate(
        store, experiment_id=plan.experiment_id, candidate_id=plan.candidate_id, at=plan.recorded_at
    )
    if candidate.heldout_mse >= candidate.baseline_heldout_mse:
        raise ValueError("IMPACT_REQUIRES_BETTER_READING_CANDIDATE")
    events = store.read_as_of(
        plan.experiment_id, now, maximum_records=128, kinds=("ContextImpactPlan",)
    )
    for event in events:
        previous = event.decode(ContextImpactPlan)
        if previous.candidate_id == plan.candidate_id:
            if previous == plan:
                return False
            raise ValueError("IMPACT_PLAN_ALREADY_REGISTERED")
    return store.append(plan, idempotency_key="context-impact-plan:" + plan.candidate_id)


class CanonicalImpactAdmission:
    def __init__(
        self, plan: ContextImpactPlan, *, registry_path: Path, declaration_path: Path, now: datetime
    ) -> None:
        self.plan = ContextImpactPlan.model_validate(plan.model_dump())
        self.registry_path, self.declaration_path = registry_path, declaration_path
        self.now = aware(now)

    def _guard(self) -> None:
        if self.now < self.plan.evaluation_end:
            raise ValueError("IMPACT_EVALUATION_CUTOFF_NOT_REACHED")
        for path in {self.registry_path, self.declaration_path}:
            with _session(path) as session:
                guard_range(
                    session,
                    "us-equities-daily",
                    self.plan.source_start.date(),
                    self.plan.evaluation_end.date(),
                )

    def used_calls(self) -> int:
        with _session(self.registry_path) as session:
            return sum(
                i.startswith(experiment_trial_prefixes(self.plan.experiment_id))
                for i in verified_intraday_attempt_ids(session)
            )

    def charge_pair(self, request_id: str) -> tuple[str, str]:
        if not re.fullmatch("[a-f0-9]{64}", request_id):
            raise ValueError("EXACT_IMPACT_REQUEST_REQUIRED")
        self._guard()
        prefix = (
            "intraday:context-impact-"
            + content_id(self.plan.experiment_id)[:16]
            + ":"
            + self.plan.plan_id[:16]
            + ":"
        )
        identities = (prefix + request_id + ":p", prefix + request_id + ":c")
        with _session(self.registry_path, write=True) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            guard_range(
                session,
                "us-equities-daily",
                self.plan.source_start.date(),
                self.plan.evaluation_end.date(),
            )
            with _session(self.declaration_path) as declarations:
                guard_range(
                    declarations,
                    "us-equities-daily",
                    self.plan.source_start.date(),
                    self.plan.evaluation_end.date(),
                )
            used = verified_intraday_attempt_ids(session)
            if any(i.startswith(prefix) for i in used):
                raise ValueError("IMPACT_FOLD_ALREADY_SPENT")
            if (
                sum(i.startswith(experiment_trial_prefixes(self.plan.experiment_id)) for i in used)
                + 2
                > 6
            ):
                raise ValueError("CANONICAL_SIX_CALL_FIT_BUDGET_EXHAUSTED")
            for identity in identities:
                record_intraday_attempt(
                    session,
                    search_id=identity,
                    configuration_hash=content_id((self.plan.plan_id, request_id, identity)),
                    start=self.plan.source_start.date(),
                    end=self.plan.evaluation_end.date(),
                    now=self.now,
                )
        return identities


def load_retained_impact_comparison(
    store: ExperimentStore,
    *,
    experiment_id: str,
    comparison_id: str,
    at: datetime,
    core_store: CoreStore | None = None,
    session: Session | None = None,
) -> ContextImpactComparison:
    events = store.read_as_of(
        experiment_id,
        aware(at),
        maximum_records=4000,
        kinds=(
            "ContextImpactComparison",
            "ContextInvalidationRecord",
            "ContextOutcomeFeedback",
            "ContextOutcomeEligibility",
        ),
    )
    invalidated = {
        event.decode(ContextInvalidationRecord).invalidation.reference_id
        for event in events
        if event.kind == "ContextInvalidationRecord"
        and event.decode(ContextInvalidationRecord).invalidation.invalidated_at <= at
    }
    for event in events:
        if event.kind != "ContextImpactComparison":
            continue
        comparison = event.decode(ContextImpactComparison)
        if comparison.comparison_id == comparison_id:
            if invalidated & {
                comparison_id,
                comparison.candidate_id,
                comparison.plan_id,
                *comparison.cost_evidence_ids,
            }:
                raise ValueError("CONTEXT_IMPACT_COMPARISON_INVALIDATED")
            if core_store is not None or comparison.evidence_lane != "SYNTHETIC_TEST":
                from quantbot.learner.context_runtime_feedback import revalidate_retained_outcomes

                if (
                    core_store is None
                    or not comparison.feedback_ids
                    or comparison.core_store_identity != core_store.database.store_identity
                ):
                    raise ValueError("CURRENT_OUTCOME_REVALIDATION_REQUIRED")
                if revalidate_retained_outcomes(
                    core_store,
                    store,
                    experiment_id=experiment_id,
                    feedback_ids=set(comparison.feedback_ids),
                    as_of=at,
                    session=session,
                ):
                    raise ValueError("CONTEXT_SELECTED_OUTCOMES_UNVERIFIED")
            else:
                from quantbot.learner.context_runtime_feedback import (
                    ContextOutcomeFeedback,
                    _eligibility_states,
                )

                states = _eligibility_states(events)
                for item in events:
                    if item.kind == "ContextOutcomeFeedback":
                        row = item.decode(ContextOutcomeFeedback)
                        state = states.get((row.core_store_id, row.lot_id))
                        if (
                            row.feedback_id in comparison.feedback_ids
                            and state
                            and state.objections
                        ):
                            raise ValueError("CONTEXT_SELECTED_OUTCOMES_UNVERIFIED")
            return comparison
    raise ValueError("RETAINED_IMPACT_COMPARISON_UNAVAILABLE")


def run_registered_downstream_comparison(
    store: ExperimentStore,
    research: DiscoveryStore,
    *,
    experiment_id: str,
    candidate_id: str,
    registry_path: Path,
    declaration_path: Path,
    now: datetime | None = None,
    core_store: CoreStore | None = None,
) -> ContextImpactEvaluation:
    """Only retained prospective candidate consumption and verified outcome joins reach fitting."""
    from quantbot.learner.context_runtime_feedback import (
        load_runtime_context_feedback,
        load_shadow_consumption,
        revalidate_retained_outcomes,
        runtime_downstream_examples,
    )

    now = aware(now or datetime.now(UTC))
    events = store.read_as_of(
        experiment_id,
        now,
        maximum_records=4000,
        kinds=("ContextImpactPlan", "ContextImpactEvaluation", "ContextImpactComparison"),
    )
    plans = [
        e.decode(ContextImpactPlan)
        for e in events
        if e.kind == "ContextImpactPlan"
        and e.decode(ContextImpactPlan).candidate_id == candidate_id
    ]
    if len(plans) != 1:
        raise ValueError("ONE_PREDECLARED_IMPACT_PLAN_REQUIRED")
    plan = plans[0]
    if store.state(experiment_id).manifest_hash != plan.manifest_hash:
        raise ValueError("EXACT_PARENT_MANIFEST_REQUIRED")
    load_retained_candidate(store, experiment_id=experiment_id, candidate_id=candidate_id, at=now)
    if content_id(load_source_permissions(research)) != plan.source_policy_id:
        raise ValueError("IMPACT_SOURCE_PERMISSION_CHANGED")
    admission = CanonicalImpactAdmission(
        plan, registry_path=registry_path, declaration_path=declaration_path, now=now
    )
    admission._guard()  # Canonical protection before loading source/training material.
    feedback, feedback_exclusions = load_runtime_context_feedback(
        store,
        research,
        experiment_id=experiment_id,
        as_of=now,
        include_suspended=True,
    )
    eligible: list[ContextOutcomeFeedback] = []
    for row in feedback:
        if not plan.training_start <= row.decision_at <= plan.evaluation_end:
            continue
        shadow = load_shadow_consumption(research, observation_id=row.observation_id)
        if shadow is None or shadow.candidate_id != candidate_id:
            continue
        eligible.append(row)
    eligible = list(select_impact_feedback(eligible, plan))
    if any(row.recorded_at > plan.evaluation_end for row in eligible):
        raise ValueError("IMPACT_LABEL_CHANGED_AFTER_FROZEN_CUTOFF")
    if core_store is None and {row.feedback_id for row in eligible} & {
        identity
        for identity, reason in feedback_exclusions
        if reason == "OUTCOME_ELIGIBILITY_SUSPENDED"
    }:
        raise ValueError("CONTEXT_SELECTED_OUTCOMES_UNVERIFIED")
    decision_ids = {row.decision_id for row in eligible}
    observations = []
    with closing(sqlite3.connect(registry_path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        for row in eligible:
            found = connection.execute(
                "SELECT payload FROM core_observations WHERE id=?", (row.observation_id,)
            ).fetchone()
            if found is not None:
                observations.append(Observation.model_validate_json(found[0]))
    if core_store is not None or any(row.evidence_lane != "SYNTHETIC_TEST" for row in observations):
        if core_store is None or core_store.database.path.resolve() != registry_path.resolve():
            raise ValueError("CURRENT_OUTCOME_REVALIDATION_REQUIRED")
        if revalidate_retained_outcomes(
            core_store,
            store,
            experiment_id=experiment_id,
            feedback_ids={row.feedback_id for row in eligible},
            as_of=now,
        ):
            raise ValueError("CONTEXT_SELECTED_OUTCOMES_UNVERIFIED")
    rows, _ = runtime_downstream_examples(
        store,
        research,
        experiment_id=experiment_id,
        observations=observations,
        as_of=plan.evaluation_end,
        cost_basis=plan.cost_basis,
        modeled_cost_contract_id=plan.modeled_cost_contract_id,
        decision_ids=decision_ids,
    )
    rows = tuple(
        row
        for row in rows
        if row.decision_id in decision_ids
        and row.interpreter_id == candidate_id
        and row.consumed_context_features is not None
    )
    documents = {}
    for row in eligible:
        capture = load_context_capture(research, row.snapshot_id)
        for document in retained_context_documents(research, capture):
            if document.published_at < plan.source_start:
                raise ValueError("IMPACT_SOURCE_OUTSIDE_DECLARED_WINDOW")
            documents[document.document_id] = document
    # Fixed provenance purge after the chronological cap; do not refill removed holdout rows.
    rows = purge_heldout_source_overlap(rows, documents, split_at=plan.split_at)
    retained_decisions = {row.decision_id for row in rows}
    eligible = [row for row in eligible if row.decision_id in retained_decisions]
    request_id = content_id((plan.plan_id, rows))
    for event in events:
        if event.kind == "ContextImpactEvaluation":
            prior = event.decode(ContextImpactEvaluation)
            if prior.plan_id == plan.plan_id and prior.request_id == request_id:
                if prior.comparison_id is not None:
                    load_retained_impact_comparison(
                        store,
                        experiment_id=experiment_id,
                        comparison_id=prior.comparison_id,
                        at=now,
                        core_store=core_store,
                    )
                return prior
    train = [
        r for r in rows if r.decision_at < plan.split_at and r.label_available_at < plan.split_at
    ]
    heldout = [r for r in rows if r.decision_at >= plan.split_at]
    result = dict(
        experiment_id=experiment_id,
        recorded_at=now,
        candidate_id=candidate_id,
        plan_id=plan.plan_id,
        request_id=request_id,
        cost_basis=plan.cost_basis,
        eligible_examples=len(rows),
    )
    if len(train) < 8 or len(heldout) < 4:
        evaluation = ContextImpactEvaluation(
            **result,
            status="INSUFFICIENT_EVIDENCE",
            reason="INSUFFICIENT_PROSPECTIVE_SHADOW_OUTCOMES",
        )
    else:
        retained = next(
            (
                e.decode(ContextImpactComparison)
                for e in events
                if e.kind == "ContextImpactComparison"
                and e.decode(ContextImpactComparison).plan_id == plan.plan_id
                and e.decode(ContextImpactComparison).request_id == request_id
            ),
            None,
        )
        if retained is None:
            trial_ids = admission.charge_pair(request_id)
            comparison = compare_context_to_price_only(
                rows,
                tuple(documents.values()),
                price_feature_names=FEATURE_NAMES,
                split_at=plan.split_at,
                as_of=plan.evaluation_end,
            )
            comparison = comparison.model_copy(update={"recorded_at": now})
            retained = ContextImpactComparison(
                **{key: value for key, value in result.items() if key != "eligible_examples"},
                comparison=comparison,
                cost_evidence_ids=tuple(
                    sorted(
                        {
                            *(r.cost_evidence_id for r in rows),
                            *(
                                ref
                                for r in eligible
                                for ref in (
                                    r.feedback_id,
                                    r.outcome_id,
                                    *r.cost_evidence_ids,
                                )
                            ),
                            *(
                                (plan.modeled_cost_contract_id,)
                                if plan.modeled_cost_contract_id
                                else ()
                            ),
                        }
                    )
                ),
                trial_ids=trial_ids,
                feedback_ids=tuple(row.feedback_id for row in eligible),
                core_store_identity=eligible[0].core_store_id,
                evidence_lane="SYNTHETIC_TEST"
                if all(o.evidence_lane == "SYNTHETIC_TEST" for o in observations)
                else "BROKER_PAPER"
                if any(o.evidence_lane == "BROKER_PAPER" for o in observations)
                else "LOCAL_SIM_REAL_DATA",
            )
            store.append(retained, idempotency_key="context-impact-comparison:" + plan.plan_id)
        comparison = retained.comparison
        selected = [
            r.net_return_bps
            for r in heldout
            if predict(
                comparison.context_estimator,
                [(*r.price_features, *(r.consumed_context_features or ()))],
            )[0]
            > 0
        ]
        evaluation = ContextImpactEvaluation(
            **result,
            status="EVALUATED",
            reason="PAIRED_HELDOUT_RESEARCH_NOT_PROFITABILITY_OR_EXECUTION_AUTHORITY",
            comparison_id=retained.comparison_id,
            selected_heldout_count=len(selected),
            selected_heldout_mean_net_bps=sum(selected) / len(selected) if selected else None,
        )
    store.append(evaluation, idempotency_key="context-impact-evaluation:" + content_id(evaluation))
    return evaluation
