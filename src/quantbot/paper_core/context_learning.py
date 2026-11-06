"""Bounded scheduled context evidence; registered research never grants trading authority."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_serializer
from sqlalchemy import select, text

from quantbot.learner.context_admission import (
    ContextEvaluationPlan,
    run_registered_context_evaluation,
)
from quantbot.learner.context_feedback import ReadingReview
from quantbot.learner.context_integration import journal_context_snapshot
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.models import LearnerModel
from quantbot.paper_core.context_integration import load_context_capture, load_source_permissions
from quantbot.paper_core.contracts import Decision, Observation, RoundTrip
from quantbot.paper_core.discovery import DiscoveryStore
from quantbot.paper_core.external_context import Digest, aware
from quantbot.paper_core.store import CoreStore, table
from quantbot.research.manifest import content_id

BINDING_KEY = "context:learning-binding"
STATUS_KEY = "context:learning-status"
PROGRESS_KEY = "context:learning-progress"
BATCH_SIZE = 64


class ContextLearningBinding(LearnerModel):
    contract: Literal["SCHEDULED_CONTEXT_LEARNING_V1"] = "SCHEDULED_CONTEXT_LEARNING_V1"
    experiment_id: str = Field(min_length=1)
    core_store_identity: str = Field(min_length=1)
    experiment_store_path: Path
    registry_path: Path
    declaration_path: Path
    manifest_hash: Digest
    plan_id: Digest
    recorded_at: datetime
    capture_rowid: int = Field(ge=0)
    decision_rowid: int = Field(ge=0)
    outcome_rowid: int = Field(ge=0)
    grants_activation: Literal[False] = False

    interpreter_admission: Literal["SHADOW_ONLY_ON_BETTER_READING_HOLDOUT_V1"] | None = None
    modeled_cost_contract_id: Digest | None = None

    @field_serializer("experiment_store_path", "registry_path", "declaration_path")
    def serialize_path(self, value: Path) -> str:
        return str(value)


class ContextLearningStatus(LearnerModel):
    contract: Literal["SCHEDULED_CONTEXT_LEARNING_STATUS_V1"] = (
        "SCHEDULED_CONTEXT_LEARNING_STATUS_V1"
    )
    recorded_at: datetime
    binding_id: Digest | None = None
    experiment_id: str | None = None
    state: Literal["UNAVAILABLE", "COLLECTING", "WAITING", "EVALUATED"]
    gate_counts: tuple[tuple[str, int], ...] = ()
    snapshots_journaled: int = 0
    feedback_journaled: int = 0
    independent_reviews: int = 0
    eligible_examples: int = 0
    candidate_id: str | None = None
    grants_activation: Literal[False] = False
    interpreter_mode: str | None = None
    interpreter_version_id: str | None = None
    impact_evaluation_end: datetime | None = None
    impact_status: str | None = None
    training_start: datetime | None = None
    split_at: datetime | None = None
    evaluation_end: datetime | None = None


def _save_status(research: DiscoveryStore, status: ContextLearningStatus) -> ContextLearningStatus:
    with research.connection() as db:
        old = research._get(db, STATUS_KEY)
        if old is not None:
            previous = ContextLearningStatus.model_validate(old)
            if previous.model_dump(exclude={"recorded_at"}) == status.model_dump(
                exclude={"recorded_at"}
            ):
                return previous
        research._set(db, STATUS_KEY, status)
    return status


def _source_window(research: DiscoveryStore, snapshot_id: str, plan: ContextEvaluationPlan) -> bool:
    capture = load_context_capture(research, snapshot_id)
    return bool(
        # Later snapshots feed prospective shadow outcomes. The registered semantic
        # evaluator independently freezes its own evaluation_end and cannot use them.
        plan.training_start <= capture.snapshot.at
        and all(
            b.first_seen_at >= plan.source_start
            and (b.published_at is None or b.published_at >= plan.source_start)
            for b in capture.bindings
        )
    )


def _core_batch(
    store: CoreStore, progress: dict[str, Any], *, now: datetime
) -> tuple[
    tuple[Decision, ...],
    tuple[Observation, ...],
    tuple[RoundTrip, ...],
    dict[str, tuple[str, ...]],
    Sequence[Any],
    Sequence[Any],
]:
    """Existing rowids bound work; immutable revisions requeue later corrected outcomes."""
    from quantbot.paper_core.outcomes import OutcomeIncomplete, _resolve, quarantined_lots
    from quantbot.storage import StateConflictError

    with store.database.transaction() as session:
        session.execute(text("BEGIN"))
        rows = session.execute(
            text(
                "SELECT rowid,payload FROM core_decisions WHERE rowid>:cursor "
                "ORDER BY rowid LIMIT :limit"
            ),
            {"cursor": progress["decisions"], "limit": BATCH_SIZE + 1},
        ).all()
        revisions = session.execute(
            text(
                "SELECT rowid,payload FROM core_outcome_revisions WHERE rowid>:cursor "
                "ORDER BY rowid LIMIT :limit"
            ),
            {"cursor": progress["outcomes"], "limit": BATCH_SIZE + 1},
        ).all()
        decisions = {
            d.id: d
            for _, raw in rows[:BATCH_SIZE]
            if (d := Decision.model_validate_json(raw)).recorded_at_utc <= now
        }
        trips, objections = [], {}
        quarantined = quarantined_lots(session)
        pointer, work = table("core_outcome_index"), table("core_outcome_work")
        retry_rows = (
            session.execute(
                select(table("core_outcome_revisions").c.payload)
                .join(pointer, pointer.c.outcome_id == table("core_outcome_revisions").c.id)
                .where(pointer.c.lot_id.in_(progress.get("pending_lots", [])[:BATCH_SIZE]))
                .limit(BATCH_SIZE)
            )
            .scalars()
            .all()
        )
        encoded_trips = dict.fromkeys([raw for _, raw in revisions[:BATCH_SIZE]] + list(retry_rows))
        for raw in encoded_trips:
            trip = RoundTrip.model_validate_json(raw)
            if trip.recorded_at_utc > now:
                continue
            trips.append(trip)
            encoded = session.execute(
                select(table("core_decisions").c.payload).where(
                    table("core_decisions").c.id == trip.decision_id
                )
            ).scalar_one_or_none()
            if encoded:
                decision = Decision.model_validate_json(encoded)
                decisions[decision.id] = decision
            reasons = []
            current = (
                session.execute(select(pointer).where(pointer.c.lot_id == trip.lot_id))
                .mappings()
                .one_or_none()
            )
            pending = (
                session.execute(select(work).where(work.c.lot_id == trip.lot_id))
                .mappings()
                .one_or_none()
            )
            if (
                current is None
                or current["outcome_id"] != trip.id
                or current["status"] not in {"COMPLETE", "COSTS_PENDING"}
            ):
                reasons.append("CONTEXT_OUTCOME_NOT_CURRENT")
            if pending is None or pending["version"] > pending["processed_version"]:
                reasons.append("CONTEXT_OUTCOME_CORRECTION_PENDING")
            if trip.lot_id in quarantined:
                reasons.append("CONTEXT_OUTCOME_QUARANTINED")
            try:
                resolved = _resolve(store, session, trip.lot_id, now)
                if resolved.id != trip.id:
                    reasons.append("CONTEXT_OUTCOME_SUPERSEDED")
            except (ValueError, StateConflictError, OutcomeIncomplete) as error:
                reasons.append("CONTEXT_OUTCOME_UNVERIFIED:" + type(error).__name__)
            objections[trip.id] = tuple(reasons)
        observations = []
        for decision in decisions.values():
            raw = session.execute(
                select(table("core_observations").c.payload).where(
                    table("core_observations").c.id == decision.observation_id
                )
            ).scalar_one_or_none()
            if raw:
                observations.append(Observation.model_validate_json(raw))
    return tuple(decisions.values()), tuple(observations), tuple(trips), objections, rows, revisions


def consider_context_learning(
    store: CoreStore, broker: Any, *, now: datetime, force: bool = False
) -> ContextLearningStatus:
    """Use commissioned existing stores hourly, or bounded cursor catch-up once a minute."""
    from quantbot.learner.context_runtime_feedback import journal_runtime_context_feedback

    now = aware(now)
    research = getattr(broker, "research_state", None)
    if research is None:
        return ContextLearningStatus(
            recorded_at=now,
            state="UNAVAILABLE",
            gate_counts=(("CONTEXT_RESEARCH_STORE_UNAVAILABLE", 1),),
        )
    with research.connection() as db:
        raw = research._get(db, BINDING_KEY)
        progress = research._get(db, PROGRESS_KEY, {})
        old = research._get(db, STATUS_KEY)
    if raw is None:
        return _save_status(
            research,
            ContextLearningStatus(
                recorded_at=now,
                state="UNAVAILABLE",
                gate_counts=(("CONTEXT_LEARNING_NOT_COMMISSIONED", 1),),
            ),
        )
    try:
        binding = ContextLearningBinding.model_validate(raw)
    except ValueError:
        return _save_status(
            research,
            ContextLearningStatus(
                recorded_at=now,
                state="UNAVAILABLE",
                gate_counts=(("CONTEXT_LEARNING_BINDING_INVALID", 1),),
            ),
        )
    binding_id = content_id(binding)
    if progress.get("binding_id") != binding_id:
        progress = dict(
            binding_id=binding_id,
            captures=binding.capture_rowid,
            decisions=binding.decision_rowid,
            outcomes=binding.outcome_rowid,
        )
    if not force and old and progress.get("attempted_at"):
        age = (now - datetime.fromisoformat(progress["attempted_at"])).total_seconds()
        if 0 <= age < (60 if progress.get("pending") else 3600):
            return ContextLearningStatus.model_validate(old)
    gates: Counter[str] = Counter()
    status = ContextLearningStatus(
        recorded_at=now,
        binding_id=binding_id,
        experiment_id=binding.experiment_id,
        state="WAITING",
        snapshots_journaled=progress.get("snapshots_total", 0),
        feedback_journaled=progress.get("feedback_total", 0),
    )
    try:
        if binding.recorded_at > now or any(
            not p.is_absolute() or not p.is_file() or p.is_symlink()
            for p in (
                binding.experiment_store_path,
                binding.registry_path,
                binding.declaration_path,
            )
        ):
            raise ValueError("EXISTING_COMMISSIONED_CONTEXT_PATHS_REQUIRED")
        if binding.core_store_identity != store.database.store_identity:
            raise ValueError("CONTEXT_CORE_STORE_BINDING_MISMATCH")
        experiments = ExperimentStore(binding.experiment_store_path)
        events = experiments.read_as_of(
            binding.experiment_id,
            now,
            maximum_records=4000,
            kinds=("ContextEvaluationPlan", "ReadingReview"),
        )
        plans = [
            e.decode(ContextEvaluationPlan) for e in events if e.kind == "ContextEvaluationPlan"
        ]
        if (
            len(plans) != 1
            or plans[0].plan_id != binding.plan_id
            or experiments.state(binding.experiment_id).manifest_hash != binding.manifest_hash
        ):
            raise ValueError("EXACT_COMMISSIONED_CONTEXT_PLAN_REQUIRED")
        plan = plans[0]
        status = status.model_copy(
            update=dict(
                training_start=plan.training_start,
                split_at=plan.split_at,
                evaluation_end=plan.evaluation_end,
            )
        )
        scope = getattr(plan, "reading_scope", None)
        if scope is None or scope.source_policy_id != content_id(load_source_permissions(research)):
            raise ValueError("CONTEXT_READING_SCOPE_OR_SOURCE_POLICY_UNVERIFIED")
        reviews = sum(
            e.kind == "ReadingReview"
            and e.decode(ReadingReview).verdict != "INCONCLUSIVE"
            and e.decode(ReadingReview).label_authority != "OUTCOME_ONLY"
            and e.decode(ReadingReview).category == "SENTIMENT_READING"
            for e in events
        )
        if not reviews:
            gates["INDEPENDENT_REVIEW_UNAVAILABLE"] += 1
        with research.connection() as db:
            captures = db.execute(
                "SELECT rowid,payload FROM receipts WHERE rowid>? ORDER BY rowid LIMIT ?",
                (progress["captures"], BATCH_SIZE + 1),
            ).fetchall()
        snapshots = 0
        for _, encoded in captures[:BATCH_SIZE]:
            capture = json.loads(encoded)
            if capture.get("kind") != "OBSERVED_CONTEXT_CAPTURE_V1":
                continue
            identity = content_id(capture["snapshot"])
            if _source_window(research, identity, plan):
                snapshots += journal_context_snapshot(
                    experiments,
                    research,
                    experiment_id=binding.experiment_id,
                    snapshot_id=identity,
                    recorded_at=now,
                )
            else:
                gates["CONTEXT_OUTSIDE_DECLARED_SOURCE_WINDOW"] += 1
        decisions, observations, trips, objections, rows, revisions = _core_batch(
            store, progress, now=now
        )
        eligible_observations = tuple(
            o
            for o in observations
            if o.context_snapshot_id and _source_window(research, o.context_snapshot_id, plan)
        )
        eligible_ids = {o.id for o in eligible_observations}
        eligible_decisions = tuple(d for d in decisions if d.observation_id in eligible_ids)
        from quantbot.paper_core.context_interpreter import admit_shadow, modeled_contract

        report = journal_runtime_context_feedback(
            experiments,
            research,
            experiment_id=binding.experiment_id,
            core_store_id=store.database.store_identity,
            decisions=eligible_decisions,
            observations=eligible_observations,
            round_trips=trips,
            as_of=now,
            outcome_objections=objections,
            modeled_cost_contract=modeled_contract(research, binding),
        )
        gates.update(dict(report.gate_counts))
        progress["pending_lots"] = sorted(
            {
                trip.lot_id
                for trip in trips
                if "CONTEXT_OUTCOME_CORRECTION_PENDING" in objections[trip.id]
            }
        )[:BATCH_SIZE]
        evaluation = None
        if now >= plan.evaluation_end:
            evaluation = run_registered_context_evaluation(
                experiments,
                research,
                experiment_id=binding.experiment_id,
                registry_path=binding.registry_path,
                declaration_path=binding.declaration_path,
                now=now,
            )
            if evaluation.status != "EVALUATED":
                gates[evaluation.reason] += 1
        else:
            gates["PREDECLARED_EVALUATION_CUTOFF_NOT_REACHED"] += 1
        from quantbot.learner.context_impact import (
            ContextImpactPlan,
            run_registered_downstream_comparison,
        )
        from quantbot.paper_core.context_interpreter import current_version

        version = (
            admit_shadow(research, experiments, binding, evaluation, now=now)
            if evaluation
            else current_version(research)
        )
        if version is not None:
            impact_plans = experiments.read_as_of(
                binding.experiment_id, now, maximum_records=4000, kinds=("ContextImpactPlan",)
            )
            impact_plan = next(
                (
                    e.decode(ContextImpactPlan)
                    for e in impact_plans
                    if e.decode(ContextImpactPlan).plan_id == version.impact_plan_id
                ),
                None,
            )
            impact = None
            if impact_plan is not None and now >= impact_plan.evaluation_end:
                impact = run_registered_downstream_comparison(
                    experiments,
                    research,
                    core_store=store,
                    experiment_id=binding.experiment_id,
                    candidate_id=version.candidate_id,
                    registry_path=binding.registry_path,
                    declaration_path=binding.declaration_path,
                    now=now,
                )
                if impact.status != "EVALUATED":
                    gates[impact.reason] += 1
            status = status.model_copy(
                update=dict(
                    interpreter_mode=version.mode,
                    interpreter_version_id=version.version_id,
                    impact_evaluation_end=impact_plan.evaluation_end if impact_plan else None,
                    impact_status=impact.status if impact else "PROSPECTIVE_COLLECTION",
                )
            )
        journal = experiments.read_as_of(
            binding.experiment_id,
            now,
            maximum_records=4000,
            kinds=("ContextSnapshotRecord", "ContextOutcomeFeedback"),
        )
        progress["snapshots_total"] = sum(e.kind == "ContextSnapshotRecord" for e in journal)
        progress["feedback_total"] = sum(e.kind == "ContextOutcomeFeedback" for e in journal)
        status = status.model_copy(
            update=dict(
                state="EVALUATED"
                if evaluation and evaluation.status == "EVALUATED"
                else "COLLECTING",
                snapshots_journaled=progress["snapshots_total"],
                feedback_journaled=progress["feedback_total"],
                independent_reviews=reviews,
                eligible_examples=evaluation.eligible_examples if evaluation else 0,
                candidate_id=evaluation.candidate_id if evaluation else None,
            )
        )
        for key, batch in (("captures", captures), ("decisions", rows), ("outcomes", revisions)):
            if batch:
                progress[key] = batch[min(BATCH_SIZE, len(batch)) - 1][0]
        progress["pending"] = bool(progress["pending_lots"]) or any(
            len(batch) > BATCH_SIZE for batch in (captures, rows, revisions)
        )
    except (ValueError, OSError, sqlite3.Error) as error:
        gates[str(error) or type(error).__name__] += 1
    status = status.model_copy(update={"gate_counts": tuple(sorted(gates.items()))})
    progress["attempted_at"] = now.isoformat()
    with research.connection() as db:
        research._set(db, PROGRESS_KEY, progress)
    return _save_status(research, status)
