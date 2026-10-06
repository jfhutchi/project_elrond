"""Isolated scheduled PAPER learning inputs; no order, fit or promotion API.

Market labels include rejected entry forecasts. Executed outcomes are separate,
selection-biased diagnostics. Existing guards run before every source-day read.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from quantbot.learner.estimators import FittedEstimator, predict
from quantbot.learner.five_minute import FEATURE_NAMES, FiveMinuteFeatureRow, FiveMinuteLabel
from quantbot.learner.five_minute_forward import FiveMinutePrediction
from quantbot.learner.forward import _read, _save
from quantbot.learner.intraday import _aware
from quantbot.research.manifest import content_id

Guard = Callable[[datetime, datetime], bool]
RAW_CONTRACT = "five-minute-raw-dia-return-shadow-v1"


def _economics(label: FiveMinuteLabel) -> tuple[Any, ...]:
    return (
        label.features,
        label.raw_return,
        label.benchmark_return,
        label.excess_return,
        label.evaluation_class,
    )


def _eligible(source: Path, section: str, guard: Guard) -> list[Path]:
    if not (source / "predictions").is_dir():
        raise FileNotFoundError("published observer predictions missing")
    files = []
    for folder in sorted((source / section).glob("*")):
        day = _aware(datetime.fromisoformat(folder.name + "T00:00:00+00:00"))
        if guard(day, day + timedelta(days=1) - timedelta(microseconds=1)):
            files.extend(sorted(folder.glob("*.json")))
        if len(files) > 10000:
            raise ValueError("bounded tracking archive maintenance required")
    return files


def _matched_label(
    source: Path, path: Path, now: datetime, model_ids: Sequence[str]
) -> FiveMinuteLabel:
    outcome = _read(path)
    prediction = FiveMinutePrediction.model_validate(
        _read(source / "predictions" / path.parent.name / path.name)
    )
    label = FiveMinuteLabel.model_validate(outcome["label"])
    if (
        outcome["prediction_id"] != path.stem
        or outcome["prediction_hash"] != prediction.prediction_id
        or prediction.model_id not in model_ids
        or prediction.features.symbol != "DIA"
        or label.features != prediction.features
        or outcome["evidence_class"] != "PROSPECTIVE_LOCAL"
        or outcome["account_pnl"] is not False
        or type(outcome["orders"]) is not int
        or outcome["orders"] != 0
        or label.available_at > now
        or prediction.recorded_at > now
        or label.features.feature_at.date().isoformat() != path.parent.name
    ):
        raise ValueError("market outcome lineage or availability refused")
    return label


def ingest_market_labels(
    *, source: Path, output: Path, now: datetime, guard: Guard, model_ids: Sequence[str]
) -> int:
    now = _aware(now)
    created = 0
    for path in _eligible(source, "outcomes", guard):
        label = _matched_label(source, path, now, model_ids)
        # Two baseline models often share exactly one target: train it once.
        document = dict(
            label=label.model_dump(mode="json"),
            feature_identity=content_id(label.features),
            selection="ALL_VALID_FORECASTS_NOT_ONLY_EXECUTED_TRADES",
            interpretation="MARKET_LABEL_NOT_EXECUTED_ACCOUNT_PNL",
            account_pnl=False,
            order_authority=False,
        )
        destination = output / (content_id(label.features) + ".json")
        if destination.exists():
            prior = FiveMinuteLabel.model_validate(_read(destination)["label"])
            if _economics(prior) != _economics(label):
                raise ValueError("learning label journal conflict")
        else:
            _save(destination, document)
            created += 1
        # Alternate resolution receipts are provenance, not extra training examples.
        revision = output / "provenance" / (content_id(document) + ".json")
        if not revision.exists():
            _save(revision, document)
    return created


def shadow_tick(
    *,
    source: Path,
    output: Path,
    models: Sequence[dict[str, Any]],
    now: datetime,
    guard: Guard,
    model_ids: Sequence[str],
    clock_healthy: bool,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    clock_health: Callable[[datetime], bool] | None = None,
) -> dict[str, int]:
    """Freeze fresh raw-return forecasts BEFORE targets; never backdate a decision."""
    now = _aware(now)
    created = resolved = 0
    verified = []
    producer_predictions = {}
    for model in models:
        artifact = FittedEstimator.model_validate(model["artifact"])
        if (
            model["model_target"] != "raw_return"
            or model["target_contract"] != RAW_CONTRACT
            or model["order_authority"] is not False
            or artifact.feature_names != FEATURE_NAMES
            or model["family"] not in {"ridge", "quadratic_ridge"}
        ):
            raise ValueError("raw shadow model contract refused")
        verified.append((model, content_id(model), artifact))
    for path in _eligible(source, "predictions", guard):
        p = FiveMinutePrediction.model_validate(_read(path))
        if (
            p.model_id not in model_ids
            or p.features.symbol != "DIA"
            or p.features.feature_at.date().isoformat() != path.parent.name
        ):
            raise ValueError("shadow feature producer refused")
        if p.recorded_at > now:
            raise ValueError("future producer forecast refused")
        producer_predictions[p.prediction_id] = p
        for model, identity, artifact in verified:
            key = content_id([identity, p.features])
            destination = output / "predictions" / path.parent.name / (key + ".json")
            if destination.exists():
                continue
            if not clock_healthy or not (
                _aware(datetime.fromisoformat(model["created_at"])) <= p.features.decision_at
                and p.recorded_at <= now < p.features.target_at
                and timedelta(0) <= now - p.features.feature_at <= timedelta(minutes=2)
            ):
                continue
            score = predict(artifact, (p.features.values,))[0]
            recorded_at = _aware(clock())
            if clock_health is not None and not clock_health(recorded_at):
                continue
            if not (
                p.recorded_at <= recorded_at <= p.features.feature_at + timedelta(minutes=2)
                and recorded_at < p.features.target_at
            ):
                continue
            _save(
                destination,
                dict(
                    model_id=identity,
                    target_contract=RAW_CONTRACT,
                    features=p.features.model_dump(mode="json"),
                    recorded_at=recorded_at.isoformat(),
                    score=score,
                    fixed_cost_floor=0.001,
                    order_authority=False,
                    source_prediction_id=p.prediction_id,
                    feature_identity=content_id(p.features),
                ),
            )
            created += 1
    # Reuse only typed, genuinely observed elapsed targets, not simulated prices.
    labels: dict[str, FiveMinuteLabel] = {}
    for path in _eligible(source, "outcomes", guard):
        label = _matched_label(source, path, now, model_ids)
        key = content_id(label.features)
        if key in labels and _economics(labels[key]) != _economics(label):
            raise ValueError("conflicting economic labels for one target")
        if key not in labels or label.available_at < labels[key].available_at:
            labels[key] = label
    for path in (
        _eligible(output, "predictions", guard) if (output / "predictions").exists() else []
    ):
        archived = _read(path)
        features = FiveMinuteFeatureRow.model_validate(archived["features"])
        producer = producer_predictions.get(archived["source_prediction_id"])
        candidates = [
            (model, artifact)
            for model, identity, artifact in verified
            if identity == archived["model_id"]
        ]
        if not candidates or producer is None:
            raise ValueError("raw shadow model or producer identity refused")
        model, artifact = candidates[0]
        recorded = _aware(datetime.fromisoformat(archived["recorded_at"]))
        if (
            archived["target_contract"] != RAW_CONTRACT
            or archived["order_authority"] is not False
            or archived["fixed_cost_floor"] != 0.001
            or not math.isfinite(archived["score"])
            or features != producer.features
            or archived["feature_identity"] != content_id(features)
            or path.stem != content_id([archived["model_id"], features])
            or path.parent.name != features.feature_at.date().isoformat()
            or not (
                _aware(datetime.fromisoformat(model["created_at"]))
                <= features.decision_at
                <= producer.recorded_at
                <= recorded
                <= max(now, _aware(clock()))
                and recorded <= features.feature_at + timedelta(minutes=2)
                and recorded < features.target_at
            )
            or archived["score"] != predict(artifact, (features.values,))[0]
        ):
            raise ValueError("raw shadow forward provenance refused")
        destination = output / "outcomes" / path.parent.name / path.name
        if destination.exists() or archived["feature_identity"] not in labels:
            continue
        label = labels[archived["feature_identity"]]
        if not (
            _aware(datetime.fromisoformat(archived["recorded_at"]))
            < label.features.target_at
            <= label.available_at
            <= now
            and archived["features"] == label.features.model_dump(mode="json")
            and archived["model_id"] in {identity for _, identity, _ in verified}
        ):
            raise ValueError("raw shadow outcome lineage refused")
        _save(
            destination,
            dict(
                prediction_hash=content_id(archived),
                label=label.model_dump(mode="json"),
                target_contract=RAW_CONTRACT,
                squared_raw_return_error=(archived["score"] - label.raw_return) ** 2,
                reference_mark_net_return=(1 + label.raw_return) * 0.9995 / 1.0005 - 1,
                interpretation="FORWARD_SHADOW_MARKS_NOT_BROKER_PNL",
                account_pnl=False,
                order_authority=False,
                orders=0,
            ),
        )
        resolved += 1
    return dict(predictions=created, outcomes=resolved)
