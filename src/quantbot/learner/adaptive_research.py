"""Two precharged shadow fits from permitted, matured chronological labels.

No active model mutation, order authority, provider access or promotion. Failed
attempts remain charged. Validation is explicitly provisional until sufficient
future completed sessions exist; price-mark simulations are never broker P&L.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from statistics import fmean
from typing import Any

from quantbot.learner.estimators import fit_estimator, predict
from quantbot.learner.five_minute import FEATURE_NAMES, FiveMinuteLabel
from quantbot.learner.forward import _save
from quantbot.learner.intraday import _aware
from quantbot.research.manifest import content_id

FAMILIES = ("ridge", "quadratic_ridge")


def chronological_inputs(
    *,
    historical: Sequence[FiveMinuteLabel],
    forward: Sequence[FiveMinuteLabel],
    historical_target_cutoff: datetime,
    training_cutoff: datetime,
    now: datetime,
) -> tuple[tuple[FiveMinuteLabel, ...], tuple[FiveMinuteLabel, ...]]:
    """Separate market event time from the time an archived outcome was retrieved.

    Historical split follows target time; all training outcomes must still have
    been available before the forward training cutoff. No timestamp is rewritten.
    Caller must guard the declared source ranges before loading these records.
    """
    historical_target_cutoff = _aware(historical_target_cutoff)
    training_cutoff, now = _aware(training_cutoff), _aware(now)
    historical = tuple(FiveMinuteLabel.model_validate(r.model_dump()) for r in historical)
    forward = tuple(FiveMinuteLabel.model_validate(r.model_dump()) for r in forward)
    training = tuple(
        r
        for r in historical
        if r.features.target_at < historical_target_cutoff and r.available_at < training_cutoff
    )
    training += tuple(r for r in forward if r.available_at < training_cutoff)
    validation = tuple(
        r for r in forward if r.features.decision_at >= training_cutoff and r.available_at <= now
    )
    return training, validation


def fit_shadow_challengers(
    *,
    root: Path,
    training: Sequence[FiveMinuteLabel],
    validation: Sequence[FiveMinuteLabel],
    cutoff: datetime,
    now: datetime,
    guard: Callable[[datetime, datetime], bool],
    charge: Callable[[str], None],
) -> dict[str, Any]:
    """Caller supplies canonical guards and permanent pre-engine accounting.

    This is a one-shot experiment directory, including on failed fitting. No
    final refit is performed; two families means exactly two engine calls.
    """
    now, cutoff = _aware(now), _aware(cutoff)
    if root.exists():
        raise FileExistsError("EXPERIMENT_EXISTS_NO_REPEAT_OR_REFIT")
    if not training or not validation or cutoff >= now:
        raise ValueError("mature training and chronological validation required")
    rows = [FiveMinuteLabel.model_validate(r.model_dump()) for r in (*training, *validation)]
    identities = [(r.features.symbol, r.features.decision_at) for r in rows]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate or overlapping label identity")
    if len({r.features.symbol for r in rows}) != 1:
        raise ValueError("frozen one-instrument study required")
    for row in rows:
        if row.available_at > now or not guard(
            row.features.feature_at - timedelta(minutes=35), row.features.target_at
        ):
            raise ValueError("future or protected evidence refused before fitting")
    if any(r.available_at >= cutoff for r in training) or any(
        r.features.decision_at < cutoff for r in validation
    ):
        raise ValueError("chronological train/validation separation required")
    spec = {
        "kind": "TWO_CHALLENGER_SHADOW_V1",
        "families": FAMILIES,
        "alpha": 1.0,
        "seed": 0,
        "training_hash": content_id(training),
        "validation_hash": content_id(validation),
        "cutoff": cutoff.isoformat(),
        "frozen_at": now.isoformat(),
        "feature_names": FEATURE_NAMES,
        "fixed_cost_floor": 0.001,
        "order_authority": False,
        "active_model_changed": False,
        "validation_interpretation": "SEEN_CHRONOLOGICAL_DIAGNOSTIC_NOT_FRESH_POST_FIT_EVIDENCE",
        "engine_source_hash": content_id(Path(__file__).read_text(encoding="utf-8")),
    }
    root.mkdir(parents=True, exist_ok=False)
    experiment_id = content_id(spec)
    _save(root / "spec.json", spec)
    trial_ids = [
        "intraday:trade-learning-" + content_id({"experiment": experiment_id, "family": f})
        for f in FAMILIES
    ]
    # Both charges committed before any numerical fitting, never refunded.
    for trial_id in trial_ids:
        charge(trial_id)
    _save(root / "charged.json", {"trial_ids": trial_ids, "experiment_id": experiment_id})
    candidates = []
    ordered_validation = sorted(validation, key=lambda r: r.features.decision_at)
    for family, trial_id in zip(FAMILIES, trial_ids, strict=True):
        artifact = fit_estimator(
            tuple(r.features.values for r in training),
            tuple(r.excess_return for r in training),
            FEATURE_NAMES,
            family=family,
            alpha=1.0,
            seed=0,
        )
        scores = predict(artifact, tuple(r.features.values for r in ordered_validation))
        last_exit = None
        opportunities = []
        for row, score in zip(ordered_validation, scores, strict=True):
            if score <= 0.001 or (last_exit is not None and row.features.decision_at < last_exit):
                continue
            last_exit = row.features.target_at
            opportunities.append(
                {
                    "decision_at": row.features.decision_at.isoformat(),
                    "score": score,
                    "target_at": row.features.target_at.isoformat(),
                    "reference_mark_net_return": (1 + row.raw_return) * 0.9995 / 1.0005 - 1,
                }
            )
        candidate = {
            "family": family,
            "artifact": artifact.model_dump(mode="json"),
            "trial_id": trial_id,
            "experiment_id": experiment_id,
            "created_at": now.isoformat(),
            "order_authority": False,
            "training_hash": spec["training_hash"],
            "validation_hash": spec["validation_hash"],
            "validation_mse": fmean(
                (s - r.excess_return) ** 2 for s, r in zip(scores, ordered_validation, strict=True)
            ),
            "cost_qualified_nonoverlapping_windows": len(opportunities),
            "counterfactual_windows": opportunities,
            "broker_fills": False,
            "cost_assumption": "5_BPS_EACH_SIDE_REFERENCE_MARKS_NOT_EXECUTABLE_FILLS",
        }
        _save(root / (family + ".json"), candidate)
        candidates.append(candidate)
    result = {
        "experiment_id": experiment_id,
        "candidates": candidates,
        "engine_calls": 2,
        "training_labels": len(training),
        "training_forward_labels": sum(r.evaluation_class == "PROSPECTIVE_LOCAL" for r in training),
        "validation_labels": len(validation),
        "validation_completed_sessions": 0,
        "validation_status": "PROVISIONAL_INSUFFICIENT_FORWARD_SESSIONS",
        "fresh_post_fit_sessions_required": 5,
        "active_model_changed": False,
        "orders": 0,
        "promotions": 0,
    }
    _save(root / "result.json", result)
    return result
