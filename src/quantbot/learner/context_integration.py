"""Retained context and independent reviews feed bounded, inactive experiment evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from quantbot.learner.context_feedback import (
    InterpreterCandidate,
    ReadingReview,
    SourceInvalidation,
    build_examples,
    candidate_usable,
    fit_interpreter_candidate,
)
from quantbot.learner.experiment_contract import CanonicalGuard, Digest, TrialCharger
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.models import LearnerModel
from quantbot.paper_core.context_integration import (
    load_context_capture,
    retained_context_documents,
)
from quantbot.paper_core.discovery import DiscoveryStore
from quantbot.paper_core.external_context import SourcePermissions, aware
from quantbot.research.manifest import content_id

CONTEXT_EVENT_KINDS = (
    "ExperimentManifest",
    "ContextEvaluationPlan",
    "ContextPlanSupersession",
    "ContextSnapshotRecord",
    "ContextInvalidationRecord",
    "ContextPermissionRecord",
    "ReadingReview",
    "ContextEvaluation",
    "ContextFitReservation",
    "InterpreterCandidate",
)


class ContextSnapshotRecord(LearnerModel):
    experiment_id: str
    recorded_at: datetime
    snapshot_id: Digest
    observed_only: Literal[True] = True


class ContextInvalidationRecord(LearnerModel):
    experiment_id: str
    recorded_at: datetime
    invalidation: SourceInvalidation


class ContextPermissionRecord(LearnerModel):
    experiment_id: str
    recorded_at: datetime
    document_id: Digest
    permission: SourcePermissions


class ContextEvaluation(LearnerModel):
    experiment_id: str
    recorded_at: datetime
    split_at: datetime
    request_id: Digest
    evaluation_plan_id: Digest | None = None
    status: Literal["EVALUATED", "INSUFFICIENT_EVIDENCE", "ADMISSION_REQUIRED"]
    reason: str
    eligible_examples: int = Field(ge=0)
    exclusions: tuple[tuple[str, str], ...] = ()
    candidate_id: Digest | None = None
    grants_activation: Literal[False] = False


class ContextFitReservation(LearnerModel):
    experiment_id: str
    recorded_at: datetime
    request_id: Digest
    trial_id: str
    manifest_hash: Digest


def journal_context_snapshot(
    store: ExperimentStore,
    research: DiscoveryStore,
    *,
    experiment_id: str,
    snapshot_id: str,
    recorded_at: datetime,
) -> bool:
    capture = load_context_capture(research, snapshot_id)
    if capture.snapshot.at > aware(recorded_at) or capture.snapshot.used_document_ids:
        raise ValueError("ONLY_AVAILABLE_OBSERVED_CONTEXT_CAN_BE_JOURNALED")
    for event in store.read_as_of(
        experiment_id, recorded_at, maximum_records=4000, kinds=("ContextSnapshotRecord",)
    ):
        if event.kind == ContextSnapshotRecord.__name__:
            if event.decode(ContextSnapshotRecord).snapshot_id == snapshot_id:
                return False
    return store.append(
        ContextSnapshotRecord(
            experiment_id=experiment_id, recorded_at=recorded_at, snapshot_id=snapshot_id
        ),
        idempotency_key="context-snapshot:" + snapshot_id,
    )


def journal_invalidation(
    store: ExperimentStore,
    *,
    experiment_id: str,
    reference_id: str,
    at: datetime,
    reason: Literal["DELETED", "REDACTED", "RIGHTS_REVOKED", "EVIDENCE_SUPERSEDED"],
) -> bool:
    record = ContextInvalidationRecord(
        experiment_id=experiment_id,
        recorded_at=at,
        invalidation=SourceInvalidation(
            reference_id=reference_id, invalidated_at=at, reason=reason
        ),
    )
    return store.append(record, idempotency_key="context-invalidation:" + content_id(record))


def evaluate_retained_context(
    store: ExperimentStore,
    research: DiscoveryStore,
    *,
    experiment_id: str,
    split_at: datetime,
    as_of: datetime,
    guard: CanonicalGuard | None = None,
    charger: TrialCharger | None = None,
    evaluation_plan_id: str | None = None,
    training_start: datetime | None = None,
    recorded_at: datetime | None = None,
    teacher_policy_id: str | None = None,
) -> ContextEvaluation:
    """Callable learner operation. No reviews, permission or fit admission are manufactured."""
    split_at, as_of = aware(split_at), aware(as_of)
    recorded_at = aware(recorded_at or as_of)
    if recorded_at < as_of:
        raise ValueError("EVALUATION_CANNOT_PRECEDE_EVIDENCE_CUTOFF")
    if not split_at < as_of:
        raise ValueError("CHRONOLOGICAL_CONTEXT_SPLIT_REQUIRED")
    state = store.state(experiment_id)
    events = store.read_as_of(
        experiment_id, recorded_at, maximum_records=4000, kinds=CONTEXT_EVENT_KINDS
    )
    captures, invalidations = [], []
    reviews = [
        e.decode(ReadingReview)
        for e in events
        if e.kind == ReadingReview.__name__ and e.recorded_at <= as_of
    ]
    reviews = [
        review
        for review in reviews
        if (
            review.label_authority == "MODEL_GENERATED"
            and review.teacher_policy_id == teacher_policy_id
            if teacher_policy_id is not None
            else review.label_authority in {"HUMAN_REVIEW", "SOURCE_CORRECTION"}
        )
    ]
    reviewed_ids = {review.reading_id for review in reviews}
    training_permissions = {}
    for event in events:
        if event.recorded_at > as_of:
            continue
        if event.kind == ContextSnapshotRecord.__name__:
            if not reviewed_ids:
                continue
            record = event.decode(ContextSnapshotRecord)
            capture = load_context_capture(research, record.snapshot_id)
            if capture.snapshot.at > record.recorded_at:
                raise ValueError("CONTEXT_CAPTURE_RECORDED_BEFORE_AVAILABLE")
            if (
                training_start is None or aware(training_start) <= capture.snapshot.at <= as_of
            ) and any(reading.reading_id in reviewed_ids for reading in capture.snapshot.readings):
                captures.append(capture)
        elif event.kind == ContextInvalidationRecord.__name__:
            invalidations.append(event.decode(ContextInvalidationRecord).invalidation)
        elif event.kind == ContextPermissionRecord.__name__:
            permission_record = event.decode(ContextPermissionRecord)
            if permission_record.permission.checked_at > permission_record.recorded_at:
                raise ValueError("PERMISSION_RECORDED_BEFORE_AVAILABLE")
            training_permissions[permission_record.document_id] = permission_record.permission
    if len(captures) > 200 or sum(len(c.snapshot.readings) for c in captures) > 2000:
        raise ValueError("CONTEXT_EVALUATION_BOUND_EXCEEDED")
    # Guard source history before opening retained article payloads for training.
    admitted = guard is not None and charger is not None
    if not admitted:
        result = ContextEvaluation(
            experiment_id=experiment_id,
            recorded_at=recorded_at,
            split_at=split_at,
            evaluation_plan_id=evaluation_plan_id,
            request_id=content_id(
                (
                    experiment_id,
                    split_at,
                    evaluation_plan_id,
                    tuple(e.record_id for e in events if e.kind != ContextEvaluation.__name__),
                )
            ),
            status="ADMISSION_REQUIRED" if reviews else "INSUFFICIENT_EVIDENCE",
            reason="CANONICAL_GUARD_AND_TRIAL_CHARGER_REQUIRED"
            if reviews
            else "NO_ELIGIBLE_INDEPENDENT_REVIEWS",
            eligible_examples=0,
        )
        for event in events:
            if event.kind == ContextEvaluation.__name__:
                old = event.decode(ContextEvaluation)
                if old.request_id == result.request_id and old.status == result.status:
                    return old
        store.append(result, idempotency_key="context-evaluation:" + content_id(result))
        return result
    assert guard is not None and charger is not None
    history_start = min(
        (
            stamp
            for capture in captures
            for stamp in (
                capture.snapshot.at,
                *(
                    binding.published_at or binding.first_seen_at
                    for binding in capture.bindings
                    if binding.kind == "NEWS_RESEARCH_V1"
                ),
            )
        ),
        default=as_of,
    )
    if captures and not guard.permits(history_start, as_of):
        raise ValueError("PROTECTED_OR_UNREGISTERED_CONTEXT_HISTORY")
    documents = tuple(d for c in captures for d in retained_context_documents(research, c))
    examples, exclusions = build_examples(
        tuple(c.snapshot for c in captures),
        documents,
        reviews,
        as_of=as_of,
        invalidations=invalidations,
        training_permissions=training_permissions,
    )
    request_id = content_id(
        dict(
            experiment_id=experiment_id,
            split_at=split_at,
            evaluation_plan_id=evaluation_plan_id,
            examples=tuple(e.example_id for e in examples),
            exclusions=exclusions,
            invalidations=invalidations,
            baseline="news-lexical-negation-v1",
        )
    )
    for event in events:
        if event.kind == ContextEvaluation.__name__:
            old = event.decode(ContextEvaluation)
            if old.request_id == request_id:
                return old
    status: Literal["EVALUATED", "INSUFFICIENT_EVIDENCE", "ADMISSION_REQUIRED"]
    status, reason, candidate = "INSUFFICIENT_EVIDENCE", "NO_ELIGIBLE_INDEPENDENT_REVIEWS", None
    train = [e for e in examples if e.decision_at < split_at and e.label_available_at < split_at]
    heldout = [e for e in examples if e.decision_at >= split_at]
    sufficient = (
        len(train) >= 8
        and len(heldout) >= 4
        and len({e.decision_at.date() for e in train}) >= 2
        and len({e.decision_at.date() for e in heldout}) >= 2
    )
    if examples and not sufficient:
        reason = "INSUFFICIENT_CHRONOLOGICAL_SOURCE_CLUSTERS"
    if sufficient:
        if state.state in {"FAULTED", "REJECTED", "COMPLETED", "INSUFFICIENT_EVIDENCE"}:
            raise ValueError("EXPERIMENT_NOT_FIT_ELIGIBLE")
        if charger.used_calls(experiment_id) >= 6:
            raise ValueError("CANONICAL_SIX_CALL_FIT_BUDGET_EXHAUSTED")
        trial_id = "intraday:context-" + content_id(experiment_id)[:16] + ":" + request_id
        reservation = ContextFitReservation(
            experiment_id=experiment_id,
            recorded_at=recorded_at,
            request_id=request_id,
            trial_id=trial_id,
            manifest_hash=state.manifest_hash,
        )
        if any(
            e.kind == ContextFitReservation.__name__
            and e.decode(ContextFitReservation).request_id == request_id
            for e in events
        ):
            raise ValueError("CONTEXT_FIT_ALREADY_RESERVED_NO_AUTOMATIC_RETRY")
        charger.charge(trial_id, state.manifest_hash)
        store.append(reservation, idempotency_key="context-fit:" + request_id)
        candidate = fit_interpreter_candidate(examples, split_at=split_at, as_of=as_of)
        candidate = InterpreterCandidate.model_validate(
            candidate.model_dump()
            | {
                "recorded_at": recorded_at,
                "evaluation": "MODEL_TEACHER_AGREEMENT_NOT_TRUTH_OR_PROFITABILITY"
                if teacher_policy_id is not None
                else candidate.evaluation,
                "lineage_refs": (*candidate.lineage_refs, evaluation_plan_id)
                if evaluation_plan_id
                else candidate.lineage_refs,
            }
        )
        store.append(candidate, idempotency_key="context-candidate:" + candidate.candidate_id)
        status, reason = "EVALUATED", "HELDOUT_READING_EVALUATION_NOT_EXECUTION_AUTHORITY"
        if teacher_policy_id is not None:
            reason = "MODEL_TEACHER_AGREEMENT_NOT_TRUTH_OR_PROFITABILITY"
    result = ContextEvaluation(
        experiment_id=experiment_id,
        recorded_at=recorded_at,
        split_at=split_at,
        evaluation_plan_id=evaluation_plan_id,
        request_id=request_id,
        status=status,
        reason=reason,
        eligible_examples=len(examples),
        exclusions=exclusions,
        candidate_id=candidate.candidate_id if candidate else None,
    )
    store.append(result, idempotency_key="context-evaluation:" + content_id(result))
    return result


def load_retained_candidate(
    store: ExperimentStore,
    *,
    experiment_id: str,
    candidate_id: str,
    at: datetime,
) -> InterpreterCandidate:
    events = store.read_as_of(
        experiment_id,
        at,
        maximum_records=4000,
        kinds=("InterpreterCandidate", "ContextInvalidationRecord"),
    )
    invalidations = tuple(
        e.decode(ContextInvalidationRecord).invalidation
        for e in events
        if e.kind == ContextInvalidationRecord.__name__
    )
    for event in events:
        if event.kind == InterpreterCandidate.__name__:
            candidate = event.decode(InterpreterCandidate)
            if candidate.candidate_id == candidate_id:
                if not candidate_usable(candidate, invalidations, at=at):
                    raise ValueError("CONTEXT_CANDIDATE_INVALIDATED")
                return candidate
    raise ValueError("CONTEXT_CANDIDATE_UNAVAILABLE")
