"""Precharged pooled PAPER fits; immutable requests never grant activation."""

from __future__ import annotations

import json
import multiprocessing
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, model_validator
from sqlalchemy import text

from quantbot.learner.estimators import FittedEstimator, fit_estimator, predict
from quantbot.learner.experiment_contract import (
    CandidateArtifact,
    CanonicalGuard,
    Digest,
    ExperimentManifest,
    ExperimentRisk,
    InstrumentIdentity,
    TrialCharger,
    validate_manifest,
)
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.five_minute import FEATURE_NAMES, FiveMinuteLabel
from quantbot.learner.models import Finite, LearnerModel, Text
from quantbot.market_data.calendar import XNYS_TIMEZONE
from quantbot.research.manifest import content_id
from quantbot.storage import SingleWriterLock


def _trial_prefix(experiment_id: str) -> str:
    return "intraday:adaptive-" + content_id(experiment_id)[:16] + ":"


def experiment_trial_prefixes(experiment_id: str) -> tuple[str, ...]:
    """Context reading fits share the registered experiment's existing six-call budget."""
    return (
        _trial_prefix(experiment_id),
        "intraday:context-" + content_id(experiment_id)[:16] + ":",
        "intraday:context-impact-" + content_id(experiment_id)[:16] + ":",
    )


class FitRequest(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    manifest_hash: Digest
    family: Literal["constant", "ridge"]
    training: tuple[FiveMinuteLabel, ...]
    validation: tuple[FiveMinuteLabel, ...]
    training_sessions: int = Field(ge=20, strict=True)
    validation_sessions: int = Field(ge=5, strict=True)
    generation: int = Field(default=0, ge=0, le=4, strict=True)
    parent_model_id: Digest | None = None
    synthetic: bool = False

    @property
    def request_id(self) -> str:
        # Repeated scheduling of the same evidence is not permission to refit it.
        return content_id(self.model_dump(exclude={"recorded_at"}))

    @property
    def trial_id(self) -> str:
        return _trial_prefix(self.experiment_id) + self.request_id

    @model_validator(mode="after")
    def lineage(self) -> Self:
        if (self.generation == 0) != (self.parent_model_id is None):
            raise ValueError("EXACT_PARENT_REQUIRED")
        if self.generation and self.family != "ridge":
            raise ValueError("SUCCESSOR_FAMILY_FROZEN")
        return self


class CanonicalTrialAlreadyReserved(ValueError):
    """Permanent registry evidence forbids an engine retry even after local state loss."""


class CanonicalTrialCharger:
    """Use the existing permanent registry burden, not a parallel luck ledger."""

    def __init__(self, request: FitRequest, *, clock: Callable[[], datetime]) -> None:
        self.request = FitRequest.model_validate(request.model_dump())
        self.clock = clock

    def used_calls(self, experiment_id: str) -> int:
        from quantbot.learner.forward import _registry_session
        from quantbot.research.intraday_accounting import verified_intraday_attempt_ids

        if experiment_id != self.request.experiment_id:
            raise ValueError("EXACT_TRIAL_EXPERIMENT_REQUIRED")
        with _registry_session() as session:
            return sum(
                identity.startswith(experiment_trial_prefixes(experiment_id))
                for identity in verified_intraday_attempt_ids(session)
            )

    def charge(self, trial_id: str, manifest_hash: str) -> None:
        from quantbot.learner.forward import _registry_session
        from quantbot.research.intraday_accounting import (
            record_intraday_attempt,
            verified_intraday_attempt_ids,
        )

        if trial_id != self.request.trial_id or manifest_hash != self.request.manifest_hash:
            raise ValueError("EXACT_TRIAL_INPUTS_REQUIRED")
        rows = (*self.request.training, *self.request.validation)
        with _registry_session(write=True) as session:
            # The canonical registry is SQLite. Its write transaction serializes roots,
            # so check-and-reserve is atomic even when their local fit locks differ.
            session.execute(text("BEGIN IMMEDIATE"))
            used = verified_intraday_attempt_ids(session)
            if trial_id in used:
                raise CanonicalTrialAlreadyReserved("CANONICAL_TRIAL_ALREADY_RESERVED")
            if (
                sum(
                    identity.startswith(experiment_trial_prefixes(self.request.experiment_id))
                    for identity in used
                )
                >= 6
            ):
                raise ValueError("CANONICAL_SIX_CALL_FIT_BUDGET_EXHAUSTED")
            record_intraday_attempt(
                session,
                search_id=trial_id,
                configuration_hash=content_id(self.request.model_dump(exclude={"recorded_at"})),
                start=min(row.features.feature_at - timedelta(minutes=35) for row in rows).date(),
                end=max(row.features.target_at for row in rows).date(),
                now=self.clock(),
            )
        # The context commits before returning; failure prevents engine invocation.


class FitStart(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    request_id: Digest
    trial_id: Text
    manifest_hash: Digest
    synthetic: bool


class FitReceipt(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    request_id: Digest
    trial_id: Text
    status: Literal["SUCCEEDED", "FAILED", "TIMEOUT", "CHARGE_REFUSED", "STARTED_UNRESOLVED"]
    charged: bool | None
    candidate_id: Digest | None = None
    validation_mse: Finite | None = None
    validation_interpretation: Literal["SEEN_DIAGNOSTIC_NOT_FRESH_FORWARD_PERFORMANCE"] = (
        "SEEN_DIAGNOSTIC_NOT_FRESH_FORWARD_PERFORMANCE"
    )
    reason: Text
    synthetic: bool


def _session(row: FiveMinuteLabel) -> str:
    return row.features.feature_at.astimezone(XNYS_TIMEZONE).date().isoformat()


def _validate_inputs(
    manifest: ExperimentManifest,
    training: Sequence[FiveMinuteLabel],
    validation: Sequence[FiveMinuteLabel],
    *,
    now: datetime,
    guard: CanonicalGuard,
    generation: int,
) -> tuple[int, int]:
    validate_manifest(manifest, guard=guard)
    if not training or not validation:
        raise ValueError("TRAINING_SESSIONS_OR_VALIDATION_MISSING")
    roster = {row.symbol for row in manifest.roster}
    identities: set[tuple[str, datetime]] = set()
    groups: list[dict[str, set[str]]] = []
    for panel in (training, validation):
        sessions: dict[str, set[str]] = {}
        for source in panel:
            row = FiveMinuteLabel.model_validate(source.model_dump())
            key = (row.features.symbol, row.features.feature_at)
            if key in identities or row.features.symbol not in roster:
                raise ValueError("DUPLICATE_OR_UNKNOWN_INSTRUMENT")
            identities.add(key)
            if row.available_at > now:
                raise ValueError("FUTURE_LABEL_REFUSED")
            if not guard.permits(
                row.features.feature_at - timedelta(minutes=35), row.features.target_at
            ):
                raise ValueError("PROTECTED_EVIDENCE_REFUSED")
            sessions.setdefault(_session(row), set()).add(row.features.symbol)
        # Frozen roster coverage, rather than choosing symbols after seeing returns.
        if any(symbols != roster for symbols in sessions.values()):
            raise ValueError("POOLED_ROSTER_SESSION_COVERAGE_INCOMPLETE")
        groups.append(sessions)
    if len(groups[0]) < manifest.minimum_training_sessions:
        raise ValueError("TRAINING_SESSIONS_INSUFFICIENT")
    if len(groups[1]) < manifest.minimum_validation_sessions:
        raise ValueError("VALIDATION_SESSIONS_INSUFFICIENT")
    if set(groups[0]) & set(groups[1]):
        raise ValueError("TRAIN_VALIDATION_SESSION_OVERLAP")
    latest_train = max(row.features.target_at for row in training)
    earliest_validation = min(row.features.feature_at for row in validation)
    if latest_train + timedelta(minutes=35) > earliest_validation:
        raise ValueError("PURGED_CHRONOLOGY_REQUIRED")
    # Offline exploratory history can be retrieved after historical validation
    # event time. Its real availability must precede this fit, never a fabricated
    # historical freeze. Forward training retains the stricter as-of boundary.
    if any(
        row.evaluation_class == "PROSPECTIVE_LOCAL" and row.available_at >= earliest_validation
        for row in training
    ):
        raise ValueError("TRAINING_NOT_AVAILABLE_BEFORE_VALIDATION")
    if generation == 0:
        for panel, start, end in (
            (training, manifest.training_start, manifest.training_end),
            (validation, manifest.validation_start, manifest.validation_end),
        ):
            if any(
                not start <= row.features.feature_at <= row.features.target_at <= end
                for row in panel
            ):
                raise ValueError("FROZEN_SOURCE_RANGE_REQUIRED")
    elif any(
        manifest.validation_start <= row.features.feature_at <= manifest.validation_end
        for row in training
    ):
        raise ValueError("SEEN_VALIDATION_CANNOT_BECOME_TRAINING")
    return len(groups[0]), len(groups[1])


def prepare_fit_request(
    manifest: ExperimentManifest,
    *,
    family: Literal["constant", "ridge"],
    training: Sequence[FiveMinuteLabel],
    validation: Sequence[FiveMinuteLabel],
    now: datetime,
    guard: CanonicalGuard,
    generation: int = 0,
    parent_model_id: str | None = None,
) -> FitRequest:
    counts = _validate_inputs(
        manifest, training, validation, now=now, guard=guard, generation=generation
    )
    return FitRequest(
        experiment_id=manifest.experiment_id,
        recorded_at=now,
        manifest_hash=content_id(manifest),
        family=family,
        training=tuple(training),
        validation=tuple(validation),
        training_sessions=counts[0],
        validation_sessions=counts[1],
        generation=generation,
        parent_model_id=parent_model_id,
        synthetic=manifest.synthetic,
    )


def load_initial_inputs(
    manifest: ExperimentManifest,
    *,
    training_path: Path,
    validation_path: Path,
    training_hash: str,
    validation_hash: str,
    guard: CanonicalGuard,
) -> tuple[tuple[FiveMinuteLabel, ...], tuple[FiveMinuteLabel, ...]]:
    # Consult both immutable canonical ranges before opening either archive.
    validate_manifest(manifest, guard=guard)

    def read(path: Path, expected: str) -> tuple[FiveMinuteLabel, ...]:
        if not path.is_absolute() or path.is_symlink() or path.stat().st_size > 32_000_000:
            raise ValueError("BOUNDED_PINNED_INPUT_REQUIRED")
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or set(document) != {"labels"}:
            raise ValueError("FROZEN_LABEL_ARCHIVE_REQUIRED")
        if content_id(document) != expected:
            raise ValueError("RESEARCH_SOURCE_PIN_MISMATCH")
        return tuple(FiveMinuteLabel.model_validate(row) for row in document["labels"])

    return read(training_path, training_hash), read(validation_path, validation_hash)


def schedule_initial_fits(
    store: ExperimentStore,
    manifest: ExperimentManifest,
    *,
    training: Sequence[FiveMinuteLabel],
    validation: Sequence[FiveMinuteLabel],
    now: datetime,
    guard: CanonicalGuard,
) -> tuple[FitRequest, ...]:
    events = store.read_as_of(manifest.experiment_id, now)
    existing = [event.decode(FitRequest) for event in events if event.kind == "FitRequest"]
    result = []
    for family in manifest.families:
        prepared = prepare_fit_request(
            manifest,
            family=family,
            training=training,
            validation=validation,
            now=now,
            guard=guard,
        )
        prior = [row for row in existing if row.generation == 0 and row.family == family]
        if prior:
            if len(prior) != 1 or prior[0].request_id != prepared.request_id:
                raise ValueError("INITIAL_SOURCE_PAIR_ALREADY_FROZEN")
            result.append(prior[0])
        else:
            store.append(prepared, idempotency_key="fit-request:" + prepared.request_id)
            result.append(prepared)
    return tuple(result)


def research_eligibility(
    store: ExperimentStore,
    manifest: ExperimentManifest,
    *,
    now: datetime,
) -> FitRequest | None:
    state = store.state(manifest.experiment_id)
    if state.manifest_hash != content_id(manifest):
        raise ValueError("FROZEN_MANIFEST_MISMATCH")
    if state.state in {"FAULTED", "COMPLETED", "REJECTED", "INSUFFICIENT_EVIDENCE"}:
        return None
    events = store.read_as_of(manifest.experiment_id, now)
    starts = {event.decode(FitStart).request_id for event in events if event.kind == "FitStart"}
    if len(starts) >= manifest.fit_budget:
        return None
    for event in events:
        if event.kind == "FitRequest":
            request = event.decode(FitRequest)
            if (
                request.manifest_hash != state.manifest_hash
                or request.synthetic != manifest.synthetic
            ):
                raise ValueError("FROZEN_FIT_REQUEST_MISMATCH")
            if request.request_id not in starts:
                return request
    return None


def _child_fit(document: str, connection: Any) -> None:
    """Only immutable already-charged numerical input reaches this child."""
    try:
        request = FitRequest.model_validate_json(document)
        artifact = fit_estimator(
            tuple(row.features.values for row in request.training),
            tuple(row.raw_return for row in request.training),
            FEATURE_NAMES,
            family="mean" if request.family == "constant" else "ridge",
            alpha=1.0,
            seed=0,
        )
        connection.send(artifact.model_dump_json())
    except Exception:
        connection.send(json.dumps({"error": "FIT_ENGINE_FAILED"}))
    finally:
        connection.close()


def _isolated_engine(request: FitRequest) -> FittedEstimator:
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_child_fit, args=(request.model_dump_json(), writer))
    process.start()
    writer.close()
    try:
        if not reader.poll(900):
            raise TimeoutError("FIT_WALL_LIMIT")
        result = reader.recv()
        return FittedEstimator.model_validate_json(result)
    finally:
        reader.close()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)


def run_registered_fit(
    store: ExperimentStore,
    manifest: ExperimentManifest,
    request: FitRequest,
    *,
    guard: CanonicalGuard,
    charger: TrialCharger,
    now: datetime,
    synthetic_engine: Callable[[FitRequest], FittedEstimator] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FitReceipt:
    if synthetic_engine is not None and (not manifest.synthetic or not request.synthetic):
        raise ValueError("SYNTHETIC_ENGINE_NOT_REAL_ROUTE")
    if clock is None:
        clock = (lambda: now) if synthetic_engine is not None else (lambda: datetime.now(UTC))
    request = FitRequest.model_validate(request.model_dump())
    if (
        request.manifest_hash != content_id(manifest)
        or request.experiment_id != manifest.experiment_id
        or request.synthetic != manifest.synthetic
        or request.recorded_at > now
        or store.state(manifest.experiment_id).manifest_hash != request.manifest_hash
    ):
        raise ValueError("FROZEN_FIT_REQUEST_MISMATCH")
    if not manifest.synthetic:
        from quantbot.learner.experiment_evaluation import evaluate_experiment

        if not evaluate_experiment(store, manifest, now=now).new_research_allowed:
            raise ValueError("EXPERIMENT_EVALUATION_FIT_WINDOW_CLOSED_OR_UNKNOWN")
    counts = _validate_inputs(
        manifest,
        request.training,
        request.validation,
        now=now,
        guard=guard,
        generation=request.generation,
    )
    if counts != (request.training_sessions, request.validation_sessions):
        raise ValueError("SESSION_COUNT_MISMATCH")
    # Dedicated process lock, not a short coordinator lease: held for the entire fit.
    lock_path = store.path.parent / ("fit-" + content_id(manifest.experiment_id) + ".lock")
    with SingleWriterLock(lock_path):
        events = store.read_as_of(manifest.experiment_id, now)
        for event in events:
            if event.kind == "FitReceipt":
                receipt = event.decode(FitReceipt)
                if receipt.request_id == request.request_id:
                    return receipt
        starts = [event.decode(FitStart) for event in events if event.kind == "FitStart"]
        saved_requests = [
            event.decode(FitRequest) for event in events if event.kind == "FitRequest"
        ]
        original = next(
            (row for row in saved_requests if row.request_id == request.request_id), None
        )
        if original is not None:
            request = original
        base = dict(
            experiment_id=manifest.experiment_id,
            recorded_at=now,
            request_id=request.request_id,
            trial_id=request.trial_id,
            synthetic=manifest.synthetic,
        )
        if any(start.request_id == request.request_id for start in starts):
            return FitReceipt(
                **base,
                status="STARTED_UNRESOLVED",
                charged=None,
                reason="PRIOR_STARTED_CALL_NEVER_REINVOKED",
            )
        if (
            len(starts) >= manifest.fit_budget
            or charger.used_calls(manifest.experiment_id) >= manifest.fit_budget
        ):
            raise ValueError("SIX_CALL_FIT_BUDGET_EXHAUSTED")
        previous_requests = [
            event.decode(FitRequest) for event in events if event.kind == "FitRequest"
        ]
        if request.generation == 0 and any(
            prior.generation == 0
            and prior.family == request.family
            and prior.request_id != request.request_id
            for prior in previous_requests
        ):
            raise ValueError("INITIAL_FAMILY_ALREADY_STARTED_NO_RETUNING")
        if request.generation == 0 and any(
            prior.generation == 0
            and (
                content_id(prior.training) != content_id(request.training)
                or content_id(prior.validation) != content_id(request.validation)
            )
            for prior in previous_requests
        ):
            raise ValueError("INITIAL_PAIR_SOURCE_ALREADY_FROZEN")
        if request.generation and any(
            prior.generation == request.generation and prior.request_id != request.request_id
            for prior in previous_requests
        ):
            raise ValueError("SUCCESSOR_GENERATION_ALREADY_STARTED")
        if request.generation:
            seen_validation = {
                (row.features.symbol, row.features.feature_at)
                for prior in previous_requests
                if prior.request_id != request.request_id
                for row in prior.validation
            }
            if any(
                (row.features.symbol, row.features.feature_at) in seen_validation
                for row in (*request.training, *request.validation)
            ):
                raise ValueError("SEEN_VALIDATION_CANNOT_BE_REFIT_OR_RESELECTED")
            parents = [
                event.decode(CandidateArtifact)
                for event in events
                if event.kind == "CandidateArtifact"
                and event.decode(CandidateArtifact).model_id == request.parent_model_id
            ]
            if len(parents) != 1 or parents[0].generation + 1 != request.generation:
                raise ValueError("VERIFIED_SUCCESSOR_PARENT_REQUIRED")
            if not any(
                event.kind == "FitReceipt"
                and event.decode(FitReceipt).status == "SUCCEEDED"
                and event.decode(FitReceipt).charged is True
                and event.decode(FitReceipt).candidate_id == request.parent_model_id
                for event in events
            ):
                raise ValueError("VERIFIED_PARENT_FIT_RECEIPT_REQUIRED")
            fresh = {
                _session(row)
                for row in (*request.training, *request.validation)
                if row.features.feature_at > parents[0].recorded_at
            }
            if len(fresh) < manifest.successor_interval_sessions:
                raise ValueError("FIVE_NEW_SUCCESSOR_SESSIONS_REQUIRED")
        state = store.state(manifest.experiment_id)
        # Research activity cannot replace an already-active exact model's authority.
        targets = {
            "REGISTERED": ("OBSERVING", "FIT_ELIGIBLE", "FIT_REGISTERED"),
            "WAITING_FOR_COVERAGE": ("OBSERVING", "FIT_ELIGIBLE", "FIT_REGISTERED"),
            "OBSERVING": ("FIT_ELIGIBLE", "FIT_REGISTERED"),
            "CANDIDATE_SHADOW": ("FIT_ELIGIBLE", "FIT_REGISTERED"),
            "FIT_ELIGIBLE": ("FIT_REGISTERED",),
            "EVALUATING": ("FIT_ELIGIBLE", "FIT_REGISTERED"),
        }
        for target in targets.get(state.state, ()):
            state = store.transition(
                manifest.experiment_id,
                expected_revision=state.revision,
                target=target,
                reason="FROZEN_POOLED_FIT_REQUEST",
                now=now,
            )
        if state.state in {"FAULTED", "COMPLETED", "REJECTED", "INSUFFICIENT_EVIDENCE"}:
            raise ValueError("EXPERIMENT_RESEARCH_STOPPED")
        store.append(request, idempotency_key="fit-request:" + request.request_id)
        store.append(
            FitStart(**base, manifest_hash=request.manifest_hash),
            idempotency_key="fit-start:" + request.request_id,
        )
        try:
            charger.charge(request.trial_id, request.manifest_hash)
        except Exception as error:
            completed = clock()
            if completed < now:
                raise ValueError("FIT_COMPLETION_CLOCK_REVERSED") from None
            base["recorded_at"] = completed
            receipt = FitReceipt(
                **base,
                status="STARTED_UNRESOLVED"
                if isinstance(error, CanonicalTrialAlreadyReserved)
                else "CHARGE_REFUSED",
                charged=True if isinstance(error, CanonicalTrialAlreadyReserved) else None,
                reason="CANONICAL_TRIAL_ALREADY_RESERVED_NO_ENGINE_CALL"
                if isinstance(error, CanonicalTrialAlreadyReserved)
                else "CANONICAL_CHARGE_REFUSED_NO_ENGINE_CALL",
            )
        else:
            try:
                artifact = (synthetic_engine or _isolated_engine)(request)
                completed = clock()
                if completed < now:
                    raise ValueError("FIT_COMPLETION_CLOCK_REVERSED")
                base["recorded_at"] = completed
                candidate = CandidateArtifact(
                    experiment_id=manifest.experiment_id,
                    recorded_at=completed,
                    manifest_hash=request.manifest_hash,
                    trial_id=request.trial_id,
                    training_source_hash=content_id(request.training),
                    validation_source_hash=content_id(request.validation),
                    artifact=artifact,
                    generation=request.generation,
                    parent_model_id=request.parent_model_id,
                    synthetic=manifest.synthetic,
                )
                expected_family = "mean" if request.family == "constant" else "ridge"
                if candidate.artifact.family != expected_family:
                    raise ValueError("FROZEN_ENGINE_FAMILY_MISMATCH")
                scores = predict(artifact, tuple(row.features.values for row in request.validation))
                mse = sum(
                    (score - row.raw_return) ** 2
                    for score, row in zip(scores, request.validation, strict=True)
                ) / len(scores)
                store.append(candidate, idempotency_key="fit-artifact:" + request.request_id)
                receipt = FitReceipt(
                    **base,
                    status="SUCCEEDED",
                    charged=True,
                    candidate_id=candidate.model_id,
                    validation_mse=mse,
                    reason="SHADOW_ARTIFACT_REQUIRES_EXACT_ACTIVATION",
                )
            except Exception as error:
                completed = clock()
                if completed < now:
                    raise ValueError("FIT_COMPLETION_CLOCK_REVERSED") from None
                base["recorded_at"] = completed
                receipt = FitReceipt(
                    **base,
                    status="TIMEOUT" if isinstance(error, TimeoutError) else "FAILED",
                    charged=True,
                    reason="CHARGED_FIT_FAILED_NO_RETRY",
                )
        store.append(receipt, idempotency_key="fit-result:" + request.request_id)
        state = store.state(manifest.experiment_id)
        if receipt.status == "SUCCEEDED" and state.state == "FIT_REGISTERED":
            store.transition(
                manifest.experiment_id,
                expected_revision=state.revision,
                target="CANDIDATE_SHADOW",
                reason="FRESH_SHADOW_EVALUATION_NOT_ACTIVATION",
                now=receipt.recorded_at,
            )
        return receipt


class ActivationPackage(LearnerModel):
    """Operator-reviewable exact artifact proposal, never an approval token."""

    experiment_id: Text
    recorded_at: datetime
    manifest_hash: Digest
    model_id: Digest
    fit_request_id: Digest
    trial_id: Text
    code_hash: Digest
    configuration_hash: Digest
    policy_hash: Digest
    training_source_hash: Digest
    validation_source_hash: Digest
    roster: tuple[InstrumentIdentity, ...]
    risk: ExperimentRisk
    owner: Text
    rollback_model_id: Digest | None
    grants_execution: Literal[False] = False
    synthetic: bool
    limitations: tuple[str, ...] = (
        "EXPERIMENTAL_PAPER_ONLY",
        "SEEN_VALIDATION_IS_NOT_FORWARD_PROFITABILITY",
        "NEW_ARTIFACT_REQUIRES_EXACT_OPERATOR_ACTIVATION",
        "LEGACY_POSITIONS_KEEP_EXISTING_EXIT_OWNER",
        "NEWS_RECOMMENDATIONS_REMAIN_SHADOW",
        "NO_AUTOMATIC_LIVE_TRANSITION",
    )

    @property
    def package_id(self) -> str:
        return content_id(self)


def activation_package(
    store: ExperimentStore,
    manifest: ExperimentManifest,
    *,
    fit: FitReceipt,
    now: datetime,
    config_hash: str,
    owner: str,
    rollback_model_id: str | None,
) -> ActivationPackage:
    events = store.read_as_of(manifest.experiment_id, now)
    if (
        fit.status != "SUCCEEDED"
        or fit.charged is not True
        or not any(
            event.kind == "FitReceipt" and event.decode(FitReceipt) == fit for event in events
        )
    ):
        raise ValueError("AUTHORITATIVE_FIT_RECEIPT_REQUIRED")
    candidates = [
        event.decode(CandidateArtifact)
        for event in events
        if event.kind == "CandidateArtifact"
        and event.decode(CandidateArtifact).model_id == fit.candidate_id
    ]
    requests = [
        event.decode(FitRequest)
        for event in events
        if event.kind == "FitRequest" and event.decode(FitRequest).request_id == fit.request_id
    ]
    starts = [
        event.decode(FitStart)
        for event in events
        if event.kind == "FitStart" and event.decode(FitStart).request_id == fit.request_id
    ]
    if len(candidates) != 1 or len(requests) != 1 or len(starts) != 1:
        raise ValueError("AUTHORITATIVE_FIT_LINEAGE_REQUIRED")
    candidate, request, start = candidates[0], requests[0], starts[0]
    if (
        candidate.manifest_hash != content_id(manifest)
        or request.manifest_hash != candidate.manifest_hash
        or start.manifest_hash != candidate.manifest_hash
        or candidate.trial_id != request.trial_id
        or request.trial_id != fit.trial_id
        or candidate.training_source_hash != content_id(request.training)
        or candidate.validation_source_hash != content_id(request.validation)
        or candidate.synthetic != manifest.synthetic
        or fit.synthetic != manifest.synthetic
        or not request.recorded_at
        <= start.recorded_at
        <= candidate.recorded_at
        <= fit.recorded_at
        <= now
    ):
        raise ValueError("AUTHORITATIVE_FIT_LINEAGE_MISMATCH")
    package = ActivationPackage(
        experiment_id=manifest.experiment_id,
        recorded_at=now,
        manifest_hash=content_id(manifest),
        model_id=candidate.model_id,
        fit_request_id=request.request_id,
        trial_id=request.trial_id,
        code_hash=manifest.code_hash,
        configuration_hash=config_hash,
        policy_hash=manifest.policy_hash,
        training_source_hash=candidate.training_source_hash,
        validation_source_hash=candidate.validation_source_hash,
        roster=manifest.roster,
        risk=manifest.risk,
        owner=owner,
        rollback_model_id=rollback_model_id,
        synthetic=manifest.synthetic,
    )
    store.append(package, idempotency_key="activation-package:" + package.package_id)
    return package
