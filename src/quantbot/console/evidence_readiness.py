"""Read-only coverage and registered adaptation receipts, never entry/fit authority."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from quantbot.learner.experiment_adaptation import AdaptationReceipt
from quantbot.learner.experiment_contract import CandidateArtifact, Digest, ExperimentManifest
from quantbot.learner.experiment_coordinator import _validate_batch
from quantbot.learner.experiment_observation import ObservationBatch
from quantbot.learner.experiment_research import FitReceipt, FitRequest, FitStart
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.models import LearnerModel, Text
from quantbot.research.manifest import content_id


class InstrumentCoverage(LearnerModel):
    symbol: Text
    status: Text = "UNKNOWN"
    reason: Text = "NO_RETAINED_COVERAGE_RECEIPT"
    recorded_at: datetime | None = None
    source_hash: Digest | None = None
    last_productive_at: datetime | None = None
    last_gap_at: datetime | None = None


class AdaptationAssessment(LearnerModel):
    recorded_at: datetime
    evidence_hash: Digest
    status: Literal["NORMAL_WAIT", "QUEUED"]
    reason: Text
    fresh_complete_sessions: int
    required_fresh_sessions: int
    parent_model_id: Digest | None = None
    request_id: Digest | None = None
    fit_status: Text | None = None
    candidate_model_id: Digest | None = None
    improvement_established: Literal[False] = False


class EvidenceReadiness(LearnerModel):
    coverage: tuple[InstrumentCoverage, ...]
    adaptation: AdaptationAssessment | None = None
    grants_execution: Literal[False] = False
    synthetic: bool


_ADAPTATION_REASONS = frozenset({
    "EXPERIMENT_NOT_RESEARCH_ACTIVE", "FIT_BUDGET_EXHAUSTED_NO_RESET",
    "EVALUATION_WINDOW_CLOSED_OR_UNKNOWN", "VERIFIED_RIDGE_PARENT_REQUIRED",
    "MAXIMUM_SUCCESSORS_REACHED", "FAILED_SUCCESSOR_GENERATION_REVIEW_REQUIRED",
    "STARTED_SUCCESSOR_REVIEW_REQUIRED", "SUCCESSOR_ALREADY_REGISTERED",
    "FRESH_TRAINING_AND_UNSEEN_VALIDATION_REQUIRED", "FIXED_FRESH_DATA_SUCCESSOR_QUEUED",
})


def read_evidence_readiness(
    store: ExperimentStore, manifest: ExperimentManifest, *, now: datetime,
) -> EvidenceReadiness:
    if now.tzinfo is None or now.utcoffset() is None or now < manifest.registered_at:
        raise ValueError("ACTUAL_AS_OF_EVALUATION_TIME_REQUIRED")
    if store.state(manifest.experiment_id).manifest_hash != content_id(manifest):
        raise ValueError("FROZEN_MANIFEST_MISMATCH")
    events = store.read_as_of(manifest.experiment_id, now)
    batches = [event.decode(ObservationBatch) for event in events
               if event.kind == "ObservationBatch"]
    for batch in batches:
        _validate_batch(manifest, batch, now)
        if batch.synthetic != manifest.synthetic:
            raise ValueError("OBSERVATION_LINEAGE_UNVERIFIED")
        if any(feature.receipt_at > batch.recorded_at
               or feature.evaluation_class != "PROSPECTIVE_LOCAL" for feature in batch.features):
            raise ValueError("OBSERVATION_LINEAGE_UNVERIFIED")
        if any(sum(feature.symbol == row.symbol for feature in batch.features) != 1
               for row in batch.coverage if row.status == "AVAILABLE"):
            raise ValueError("OBSERVATION_LINEAGE_UNVERIFIED")
    coverage = []
    for identity in manifest.roster:
        rows = [(batch, row) for batch in batches for row in batch.coverage
                if row.symbol == identity.symbol]
        rows.sort(key=lambda pair: pair[0].recorded_at)
        if not rows:
            coverage.append(InstrumentCoverage(symbol=identity.symbol))
            continue
        batch, row = rows[-1]
        reason = row.reason if row.reason in {row.status, "CAPTURE_UNAVAILABLE",
                                              "PROTECTED_OBSERVATION_WINDOW"} else "UNKNOWN"
        coverage.append(InstrumentCoverage(
            symbol=identity.symbol, status=row.status, reason=reason,
            recorded_at=batch.recorded_at, source_hash=row.source_hash,
            last_productive_at=max((b.recorded_at for b, r in rows if r.status == "AVAILABLE"),
                                   default=None),
            last_gap_at=max((b.recorded_at for b, r in rows if r.status == "MISSING"),
                            default=None),
        ))
    assessments = [event.decode(AdaptationReceipt) for event in events
                   if event.kind == "AdaptationReceipt"]
    if any(row.synthetic != manifest.synthetic for row in assessments):
        raise ValueError("ADAPTATION_LINEAGE_UNVERIFIED")
    adaptation = None
    if assessments:
        receipt = max(assessments, key=lambda row: row.recorded_at)
        if len({content_id(row) for row in assessments
                if row.recorded_at == receipt.recorded_at}) != 1:
            raise ValueError("ADAPTATION_LINEAGE_UNVERIFIED")
        fit_status = candidate_id = None
        if receipt.status == "QUEUED" or receipt.request_id is not None:
            requests = [event.decode(FitRequest) for event in events if event.kind == "FitRequest"
                        and event.decode(FitRequest).request_id == receipt.request_id]
            if (len(requests) != 1 or requests[0].manifest_hash != content_id(manifest)
                    or requests[0].parent_model_id != receipt.parent_model_id
                    or requests[0].synthetic != manifest.synthetic or requests[0].generation < 1
                    or requests[0].recorded_at > receipt.recorded_at):
                raise ValueError("SUCCESSOR_REQUEST_UNVERIFIED")
            request = requests[0]
            starts = [event.decode(FitStart) for event in events if event.kind == "FitStart"
                      and event.decode(FitStart).request_id == request.request_id]
            fits = [event.decode(FitReceipt) for event in events if event.kind == "FitReceipt"
                    and event.decode(FitReceipt).request_id == request.request_id]
            if (len(starts) > 1 or len(fits) > 1
                    or any(row.synthetic != manifest.synthetic or row.trial_id != request.trial_id
                           or row.recorded_at < request.recorded_at
                           or row.manifest_hash != content_id(manifest) for row in starts)
                    or any(row.synthetic != manifest.synthetic or row.trial_id != request.trial_id
                           or row.recorded_at < request.recorded_at for row in fits)
                    or fits and fits[0].charged is True and len(starts) != 1
                    or starts and fits and fits[0].recorded_at < starts[0].recorded_at):
                raise ValueError("SUCCESSOR_FIT_UNVERIFIED")
            fit_status = fits[0].status if fits else "STARTED" if starts else "NOT_STARTED"
            if fits and fits[0].status == "SUCCEEDED":
                if fits[0].charged is not True or len(starts) != 1:
                    raise ValueError("SUCCESSOR_FIT_UNVERIFIED")
                candidates = [event.decode(CandidateArtifact) for event in events
                              if event.kind == "CandidateArtifact"
                              and event.decode(CandidateArtifact).model_id == fits[0].candidate_id]
                if (len(candidates) != 1 or candidates[0].manifest_hash != content_id(manifest)
                        or candidates[0].trial_id != request.trial_id
                        or candidates[0].generation != request.generation
                        or candidates[0].parent_model_id != request.parent_model_id
                        or candidates[0].synthetic != manifest.synthetic
                        or candidates[0].training_source_hash != content_id(request.training)
                        or candidates[0].validation_source_hash != content_id(request.validation)
                        or not request.recorded_at <= starts[0].recorded_at
                        <= candidates[0].recorded_at
                        <= fits[0].recorded_at):
                    raise ValueError("SUCCESSOR_CANDIDATE_UNVERIFIED")
                candidate_id = candidates[0].model_id
        adaptation = AdaptationAssessment(
            recorded_at=receipt.recorded_at, evidence_hash=content_id(receipt),
            status=receipt.status,
            reason=receipt.reason if receipt.reason in _ADAPTATION_REASONS else "UNKNOWN",
            fresh_complete_sessions=receipt.fresh_complete_sessions,
            required_fresh_sessions=manifest.successor_interval_sessions
            + manifest.minimum_validation_sessions,
            parent_model_id=receipt.parent_model_id, request_id=receipt.request_id,
            fit_status=fit_status, candidate_model_id=candidate_id,
        )
    return EvidenceReadiness(coverage=tuple(coverage), adaptation=adaptation,
                             synthetic=manifest.synthetic)
