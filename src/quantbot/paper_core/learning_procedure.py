"""Pure bounded development procedure; no database, broker, calendar acquisition or activation."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from quantbot.learner.estimators import fit_estimator, predict
from quantbot.learner.five_minute import FEATURE_NAMES
from quantbot.paper_core.contracts import (
    FoldResult,
    LearningCalendar,
    LearningEpisode,
    LearningFold,
    LearningPolicy,
    ModelArtifact,
    ProcedureResult,
    SessionMetrics,
)


def predict_bps(model: ModelArtifact, features: Sequence[Mapping[str, float]]) -> tuple[float, ...]:
    """Estimator parameters and prediction outputs use bps, clipped exactly once here."""
    rows = tuple(tuple(row[n] for n in FEATURE_NAMES) for row in features)
    if model.mode == "COLLECT":
        # An explicit zero constant comparison, never relabel COLLECT as fitted.
        values = (0.0,) * len(rows)
    elif model.estimator is not None:
        values = predict(model.estimator, rows)
    elif model.fixture_authority and model.constant_bps is not None:
        values = (model.constant_bps,) * len(rows)
    else:
        raise ValueError("NONPREDICTIVE_INCUMBENT")
    return tuple(max(-100.0, min(100.0, v)) for v in values)


def _target(
    episode: LearningEpisode, target_contract: str = "owned-executed-net-return-v1"
) -> float:
    trip = episode.outcome
    if target_contract == "owned-executed-net-return-v1":
        if trip.status != "COMPLETE" or trip.net_pnl is None or trip.entry_notional <= 0:
            raise ValueError("UNKNOWN_COSTS_OR_NOTIONAL")
        pnl = trip.net_pnl
    elif target_contract == "owned-executed-paper-gross-return-v1":
        if (
            (
                trip.status != "COMPLETE"
                and not (trip.status == "COSTS_PENDING" and trip.cost_reason == "FEE_UNAVAILABLE")
            )
            or trip.entry_notional <= 0
            or not trip.reconciliation_id
        ):
            raise ValueError("INCOMPLETE_GROSS_TARGET_OR_NOTIONAL")
        pnl = trip.gross_pnl
    else:
        raise ValueError("UNSUPPORTED_TARGET_CONTRACT")
    value = float(pnl / trip.entry_notional * 10000)
    if not math.isfinite(value):
        raise ValueError("NONFINITE_EXECUTED_RETURN")
    return value


def _compatible_parent(parent: ModelArtifact, target_contract: str, policy_id: str) -> None:
    if parent.mode == "PREDICT" and (
        parent.target_contract != target_contract or parent.policy_id != policy_id
    ):
        raise ValueError("INCOMPATIBLE_INCUMBENT")


def fit_candidate(
    outcomes: tuple[LearningEpisode, ...],
    parent: ModelArtifact,
    policy: LearningPolicy,
    *,
    run_id: str = "DIRECT_SHADOW_FIT",
    sample_weights: Sequence[float] | None = None,
) -> ModelArtifact:
    """Fit only the immutable guarded join, never fixed-horizon market labels."""
    if not outcomes:
        raise ValueError("EMPTY_TRAINING")
    _compatible_parent(parent, policy.target_contract, policy.policy_id)
    for episode in outcomes:
        trip, obs = episode.outcome, episode.observation
        if (
            trip.policy_id != policy.policy_id
            or trip.dataset != policy.dataset
            or trip.evidence_lane != policy.evidence_lane
            or obs.feature_version != policy.feature_version
            or tuple(n for n, _ in obs.features) != FEATURE_NAMES
            or obs.available_at > obs.recorded_at_utc
            or obs.source_end > obs.available_at
            or obs.source_start >= obs.source_end
            or trip.source_start != obs.source_start
            or trip.source_end is None
            or trip.available_at is None
            or trip.available_at < trip.source_end
        ):
            raise ValueError("INCOMPATIBLE_OR_NONCAUSAL_INPUT")
    estimator = fit_estimator(
        tuple(tuple(v for _, v in e.observation.features) for e in outcomes),
        tuple(_target(e, policy.target_contract) for e in outcomes),
        FEATURE_NAMES,
        family=policy.family,
        alpha=policy.alpha,
        seed=policy.seed,
        sample_weights=sample_weights,
    )
    return ModelArtifact(
        mode="PREDICT",
        description=(
            "shadow owned before-fee ridge candidate"
            if policy.target_contract == "owned-executed-paper-gross-return-v1"
            else "shadow owned after-cost ridge candidate"
        ),
        feature_version=policy.feature_version,
        estimator=estimator,
        target_contract=policy.target_contract,
        policy_id=policy.policy_id,
        parent_model_id=parent.id,
        input_ids=tuple(e.outcome.id for e in outcomes),
        learning_receipt_id=run_id,
    )


def _mse(actual: Sequence[float], values: Sequence[float]) -> float:
    return math.fsum((a - v) ** 2 for a, v in zip(actual, values, strict=True)) / len(actual)


def _validation(
    candidate: ModelArtifact, parent: ModelArtifact, validation: tuple[LearningEpisode, ...]
) -> tuple[dict[str, float], tuple[SessionMetrics, ...]]:
    features = tuple(dict(e.observation.features) for e in validation)
    assert candidate.target_contract is not None and candidate.policy_id is not None
    _compatible_parent(parent, candidate.target_contract, candidate.policy_id)
    actual = tuple(_target(e, candidate.target_contract) for e in validation)
    predicted, incumbent = predict_bps(candidate, features), predict_bps(parent, features)
    assert candidate.estimator is not None
    # Constant is training mean, frozen before later validation is opened.
    constant = (candidate.estimator.intercept,) * len(validation)
    sessions = []
    for day in sorted({e.session for e in validation}):
        indexes = [i for i, e in enumerate(validation) if e.session == day]
        a, c, p, z = (
            tuple(values[i] for i in indexes) for values in (actual, predicted, incumbent, constant)
        )
        sessions.append(
            SessionMetrics(
                session=day,
                outcome_ids=tuple(validation[i].outcome.id for i in indexes),
                actual_bps=a,
                candidate_bps=c,
                incumbent_bps=p,
                constant_bps=z,
                candidate_mse=_mse(a, c),
                incumbent_mse=_mse(a, p),
                constant_mse=_mse(a, z),
            )
        )
    return {
        "candidate_mse": _mse(actual, predicted),
        "incumbent_mse": _mse(actual, incumbent),
        "constant_mse": _mse(actual, constant),
        "candidate_worst_session_mse": max(s.candidate_mse for s in sessions),
        "incumbent_worst_session_mse": max(s.incumbent_mse for s in sessions),
        "constant_worst_session_mse": max(s.constant_mse for s in sessions),
    }, tuple(sessions)


def validate_candidate(
    candidate: ModelArtifact, parent: ModelArtifact, validation: tuple[LearningEpisode, ...]
) -> dict[str, float]:
    return _validation(candidate, parent, validation)[0]


def _passes_validation(metrics: dict[str, float], improvement: float) -> bool:
    return (
        metrics["candidate_mse"] < min(metrics["incumbent_mse"], metrics["constant_mse"])
        and metrics["candidate_mse"] <= (1 - improvement) * metrics["incumbent_mse"]
        and metrics["candidate_mse"] <= (1 - improvement) * metrics["constant_mse"]
        and metrics["candidate_worst_session_mse"]
        <= min(metrics["incumbent_worst_session_mse"], metrics["constant_worst_session_mse"])
    )


def raw_weights(
    episodes: tuple[LearningEpisode, ...], calendar: LearningCalendar, cutoff: datetime
) -> tuple[float, ...]:
    completed = tuple(s.session_date for s in calendar.sequence.sessions if s.close_at <= cutoff)
    if any(e.session not in completed for e in episodes):
        raise ValueError("FIT_INPUT_OUTSIDE_COMPLETED_CALENDAR")
    recent = frozenset(completed[-10:])
    return tuple(2.0 if e.session in recent else 1.0 for e in episodes)


def run_procedure(
    folds: tuple[LearningFold, ...],
    final: tuple[LearningEpisode, ...],
    parent: ModelArtifact,
    policy: LearningPolicy,
    calendar: LearningCalendar,
    cutoff: datetime,
    run_id: str,
) -> ProcedureResult:
    """Five predeclared expanding fits, identical comparisons, then gated all-history refit."""
    if len(folds) != 5 or tuple(f.session for f in folds) != tuple(
        sorted({f.session for f in folds})
    ):
        raise ValueError("FIVE_CHRONOLOGICAL_FOLDS_REQUIRED")
    if any(e.outcome.available_at is None or e.outcome.available_at > cutoff for e in final):
        raise ValueError("FINAL_INPUT_UNAVAILABLE_AT_CUTOFF")
    results: list[FoldResult] = []
    all_actual: list[float] = []
    all_candidate: list[float] = []
    all_incumbent: list[float] = []
    all_constant: list[float] = []
    for fold in folds:
        if policy.procedure_version == "daily-paper-exploration-walk-forward-v3" and any(
            e.session != fold.session
            or e.observation.source_start < fold.cutoff
            or e.observation.available_at > cutoff
            or e.outcome.available_at is None
            or e.outcome.available_at > cutoff
            or e.outcome.source_end is None
            or e.outcome.source_end > e.outcome.available_at
            for e in fold.validation
        ):
            raise ValueError("NONCAUSAL_VALIDATION_FOLD")
        if (
            not fold.training
            or not fold.validation
            or len(fold.training) < 30
            or len({e.session for e in fold.training}) < 5
            or any(
                e.session >= fold.session
                or e.outcome.available_at is None
                or e.outcome.available_at > fold.cutoff
                or e.observation.available_at > fold.cutoff
                or e.outcome.source_end is None
                or e.outcome.source_end
                > min(v.observation.source_start for v in fold.validation)
                - timedelta(minutes=policy.embargo_minutes)
                for e in fold.training
            )
        ):
            raise ValueError("NONCAUSAL_OR_INSUFFICIENT_FOLD")
        weights = raw_weights(fold.training, calendar, fold.cutoff)
        artifact = fit_candidate(
            fold.training, parent, policy, run_id=run_id, sample_weights=weights
        )
        actual = tuple(_target(e, policy.target_contract) for e in fold.validation)
        values = predict_bps(artifact, tuple(dict(e.observation.features) for e in fold.validation))
        assert artifact.estimator is not None
        constant = (max(-100.0, min(100.0, artifact.estimator.intercept)),) * len(actual)
        for model in fold.historical_models:
            _compatible_parent(model, policy.target_contract, policy.policy_id)
        historical = tuple(
            predict_bps(model, (dict(episode.observation.features),))[0]
            for model, episode in zip(fold.historical_models, fold.validation, strict=True)
        )
        if tuple(m.id for m in fold.historical_models) != fold.historical_model_ids:
            raise ValueError("HISTORICAL_MODEL_ID_MISMATCH")
        if not (
            len(actual)
            == len(historical)
            == len(fold.historical_model_ids)
            == len(fold.historical_available_at)
            == len(fold.historical_receipt_ids)
            == len(fold.historical_acknowledgement_ids)
            == len(fold.historical_acknowledgement_available_at)
        ):
            raise ValueError("HISTORICAL_COMPARATOR_DIMENSION_MISMATCH")
        scores = SessionMetrics(
            session=fold.session,
            outcome_ids=tuple(e.outcome.id for e in fold.validation),
            actual_bps=actual,
            candidate_bps=values,
            incumbent_bps=historical,
            constant_bps=constant,
            candidate_mse=_mse(actual, values),
            incumbent_mse=_mse(actual, historical),
            constant_mse=_mse(actual, constant),
        )
        scoring = tuple(e.outcome.available_at for e in fold.validation)
        if any(t is None for t in scoring):
            raise ValueError("UNKNOWN_SCORING_AVAILABILITY")
        results.append(
            FoldResult(
                session=fold.session,
                cutoff=fold.cutoff,
                training_ids=artifact.input_ids,
                validation_ids=scores.outcome_ids,
                scoring_available_at=tuple(t for t in scoring if t is not None),
                raw_weights=weights,
                artifact=artifact,
                historical_model_ids=fold.historical_model_ids,
                historical_available_at=fold.historical_available_at,
                historical_receipt_ids=fold.historical_receipt_ids,
                historical_acknowledgement_ids=fold.historical_acknowledgement_ids,
                historical_acknowledgement_available_at=fold.historical_acknowledgement_available_at,
                scores=scores,
            )
        )
        all_actual.extend(actual)
        all_candidate.extend(values)
        all_incumbent.extend(historical)
        all_constant.extend(constant)
    sessions = tuple(r.scores for r in results)
    metrics = {
        "candidate_mse": _mse(all_actual, all_candidate),
        "incumbent_mse": _mse(all_actual, all_incumbent),
        "constant_mse": _mse(all_actual, all_constant),
        "candidate_worst_session_mse": max(s.candidate_mse for s in sessions),
        "incumbent_worst_session_mse": max(s.incumbent_mse for s in sessions),
        "constant_worst_session_mse": max(s.constant_mse for s in sessions),
    }
    passed = _passes_validation(metrics, policy.min_improvement)
    final_weights = raw_weights(final, calendar, cutoff) if passed else ()
    candidate = (
        fit_candidate(final, parent, policy, run_id=run_id, sample_weights=final_weights)
        if passed
        else None
    )
    return ProcedureResult(
        candidate=candidate,
        folds=tuple(results),
        metrics=tuple(sorted(metrics.items())),
        session_metrics=sessions,
        gates_passed=passed,
        final_raw_weights=final_weights,
    )
