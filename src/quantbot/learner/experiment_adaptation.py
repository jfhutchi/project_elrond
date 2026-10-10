"""Deterministic fresh-data successor queue; never a model-selection/promotion loop."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Literal

from quantbot.learner.experiment_contract import (
    CandidateArtifact,
    CanonicalGuard,
    Digest,
    ExperimentManifest,
)
from quantbot.learner.experiment_coordinator import ResolvedMarketLabel, feature_key
from quantbot.learner.experiment_research import (
    FitReceipt,
    FitRequest,
    FitStart,
    _session,
    prepare_fit_request,
)
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.five_minute import FiveMinuteLabel
from quantbot.learner.models import LearnerModel, Text
from quantbot.research.manifest import content_id


class AdaptationReceipt(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    status: Literal["NORMAL_WAIT", "QUEUED"]
    reason: Text
    parent_model_id: Digest | None = None
    request_id: Digest | None = None
    fresh_complete_sessions: int = 0
    grants_activation: Literal[False] = False
    synthetic: bool


def schedule_next_successor(
    store: ExperimentStore,
    manifest: ExperimentManifest,
    *,
    now: datetime,
    guard: CanonicalGuard,
) -> AdaptationReceipt:
    """Keep constant baseline frozen; update only the preregistered ridge lineage.

    Each update adds five fresh training sessions and reserves five later unseen
    validation sessions. Five sessions alone would repeat the old fit or recycle
    validation; ten is the earliest honest update under the fixed daily folds.
    Neither returns nor broker P&L choose rows, parameters or the parent.
    """
    state = store.state(manifest.experiment_id)
    if state.manifest_hash != content_id(manifest):
        raise ValueError("FROZEN_MANIFEST_MISMATCH")
    events = store.read_as_of(manifest.experiment_id, now)
    requests = [e.decode(FitRequest) for e in events if e.kind == "FitRequest"]
    fits = [e.decode(FitReceipt) for e in events if e.kind == "FitReceipt"]

    def receipt(
        reason: str, *, queued: FitRequest | None = None, parent: str | None = None, fresh: int = 0
    ) -> AdaptationReceipt:
        return AdaptationReceipt(
            experiment_id=manifest.experiment_id,
            recorded_at=now,
            status="QUEUED" if queued else "NORMAL_WAIT",
            reason=reason,
            parent_model_id=parent,
            request_id=queued.request_id if queued else None,
            fresh_complete_sessions=fresh,
            synthetic=manifest.synthetic,
        )

    if state.reason.startswith('EXACT_SWITCH_PENDING:'):
        return receipt('EXACT_SWITCH_PENDING')
    if state.state in {"FAULTED", "COMPLETED", "REJECTED", "INSUFFICIENT_EVIDENCE"}:
        return receipt("EXPERIMENT_NOT_RESEARCH_ACTIVE")
    if sum(e.kind == FitStart.__name__ for e in events) >= manifest.fit_budget:
        return receipt("FIT_BUDGET_EXHAUSTED_NO_RESET")
    if not manifest.synthetic:
        from quantbot.learner.experiment_evaluation import evaluate_experiment

        if not evaluate_experiment(store, manifest, now=now).new_research_allowed:
            return receipt("EVALUATION_WINDOW_CLOSED_OR_UNKNOWN")
    candidates = [e.decode(CandidateArtifact) for e in events if e.kind == "CandidateArtifact"]
    successful = []
    for candidate in candidates:
        parents = [
            req
            for req in requests
            if any(
                fit.status == "SUCCEEDED"
                and fit.charged is True
                and fit.candidate_id == candidate.model_id
                and fit.request_id == req.request_id
                for fit in fits
            )
            and req.family == "ridge"
        ]
        if len(parents) == 1:
            request = parents[0]
            receipts = [fit for fit in fits if fit.request_id == request.request_id]
            starts = [
                e.decode(FitStart)
                for e in events
                if e.kind == "FitStart" and e.decode(FitStart).request_id == request.request_id
            ]
            if len(receipts) != 1 or len(starts) != 1:
                raise ValueError("AUTHORITATIVE_SUCCESSOR_PARENT_REQUIRED")
            fit, start = receipts[0], starts[0]
            if (
                candidate.manifest_hash != state.manifest_hash
                or candidate.synthetic != manifest.synthetic
                or request.manifest_hash != state.manifest_hash
                or start.manifest_hash != state.manifest_hash
                or candidate.generation != request.generation
                or candidate.parent_model_id != request.parent_model_id
                or candidate.trial_id != request.trial_id
                or fit.trial_id != request.trial_id
                or start.trial_id != request.trial_id
                or candidate.artifact.family != "ridge"
                or candidate.training_source_hash != content_id(request.training)
                or candidate.validation_source_hash != content_id(request.validation)
                or not request.recorded_at
                <= start.recorded_at
                <= candidate.recorded_at
                <= fit.recorded_at
                <= now
            ):
                raise ValueError("VERIFIED_SUCCESSOR_PARENT_REQUIRED")
            successful.append((candidate, parents[0]))
    if not successful:
        return receipt("VERIFIED_RIDGE_PARENT_REQUIRED")
    generations = Counter(pair[0].generation for pair in successful)
    if any(count != 1 for count in generations.values()):
        raise ValueError("SUCCESSOR_GENERATION_CONFLICT")
    candidate, parent_request = max(successful, key=lambda pair: pair[0].generation)
    generation = candidate.generation + 1
    if generation > manifest.maximum_successors:
        return receipt("MAXIMUM_SUCCESSORS_REACHED", parent=candidate.model_id)
    reserved = [req for req in requests if req.generation == generation]
    if len(reserved) > 1:
        raise ValueError("SUCCESSOR_GENERATION_CONFLICT")
    if reserved:
        attempts = [fit for fit in fits if fit.request_id == reserved[0].request_id]
        if attempts and attempts[-1].status != "SUCCEEDED":
            return receipt("FAILED_SUCCESSOR_GENERATION_REVIEW_REQUIRED", parent=candidate.model_id)
        if not attempts and any(
            e.kind == "FitStart" and e.decode(FitStart).request_id == reserved[0].request_id
            for e in events
        ):
            return receipt("STARTED_SUCCESSOR_REVIEW_REQUIRED", parent=candidate.model_id)
        return receipt(
            "SUCCESSOR_ALREADY_REGISTERED", queued=reserved[0], parent=candidate.model_id
        )
    seen_validation_dates = {_session(label) for req in requests for label in req.validation}
    records = [e.decode(ResolvedMarketLabel) for e in events if e.kind == "ResolvedMarketLabel"]
    counts = Counter(feature_key(row.label.features) for row in records)
    cohorts: dict[tuple[str, datetime], list[FiveMinuteLabel]] = {}
    for record in records:
        row = record.label
        if (
            record.synthetic == manifest.synthetic
            and counts[feature_key(row.features)] == 1
            and row.evaluation_class == "PROSPECTIVE_LOCAL"
            and row.available_at <= now
            and row.features.feature_at > candidate.recorded_at
            and _session(row) not in seen_validation_dates
        ):
            cohorts.setdefault((_session(row), row.features.feature_at), []).append(row)
    roster = {identity.symbol for identity in manifest.roster}
    days: dict[str, tuple[FiveMinuteLabel, ...]] = {}
    for (day, _), rows in sorted(cohorts.items()):
        if (
            day not in days
            and len(rows) == len(roster)
            and {r.features.symbol for r in rows} == roster
        ):
            days[day] = tuple(sorted(rows, key=lambda row: row.features.symbol))
    minimum = manifest.successor_interval_sessions + manifest.minimum_validation_sessions
    if len(days) < minimum:
        return receipt(
            "FRESH_TRAINING_AND_UNSEEN_VALIDATION_REQUIRED",
            parent=candidate.model_id,
            fresh=len(days),
        )
    chosen = tuple(days[key] for key in sorted(days)[:minimum])
    training = (
        *parent_request.training,
        *(row for cohort in chosen[: manifest.successor_interval_sessions] for row in cohort),
    )
    validation = tuple(
        row for cohort in chosen[manifest.successor_interval_sessions :] for row in cohort
    )
    queued = prepare_fit_request(
        manifest,
        family="ridge",
        training=training,
        validation=validation,
        now=now,
        guard=guard,
        generation=generation,
        parent_model_id=candidate.model_id,
    )
    store.append(queued, idempotency_key="fit-request:" + queued.request_id)
    return receipt(
        "FIXED_FRESH_DATA_SUCCESSOR_QUEUED",
        queued=queued,
        parent=candidate.model_id,
        fresh=len(days),
    )
