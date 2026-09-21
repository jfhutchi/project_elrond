"""Supervised research lifecycle with explicit, separately committed evidence stages."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import mean
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from quantbot.learner.data import DailyInput, Dataset, FeatureRow
from quantbot.learner.estimators import fit_estimator, predict
from quantbot.learner.metrics import ScoredRow, evaluate_predictions
from quantbot.learner.models import Evaluation, EvaluationClass, ModelVersion, Outcome, Prediction
from quantbot.learner.repository import LearnerRepository
from quantbot.research.datasets import observation_identity
from quantbot.research.manifest import canonical_result_json, content_id
from quantbot.research.registry import DataRole, HypothesisRegistry
from quantbot.research.reproducibility import capture_environment, capture_git_state
from quantbot.storage import Database, RecordNotFoundError
from quantbot.storage.database import encode_utc
from quantbot.storage.schema import learner_datasets, search_runs


def _utcnow() -> datetime:
    return datetime.now(UTC)


def guard_range(session: Session, dataset: str, start: date, end: date) -> None:
    """Refuse registered protected ranges, including vendor aliases, before raw data access.

    Learner declarations also prevent later repartitioning from recycling their holdout. They
    grant no access or fresh-evidence claim; protected measurement still needs ExperimentRunner.
    """
    if HypothesisRegistry(session).blocking_protected_windows(
        dataset, start, end, now=_utcnow().date(), include_forward_paper=True
    ):
        raise ValueError(
            "protected or forward-paper range requires the existing ExperimentRunner access path"
        )
    # Intentionally stronger than an expiring reservation: no ordinary learner command unseals
    # a declared holdout or consumes it for debugging. These are manifests, not a second issuer.
    for identity in session.execute(select(learner_datasets.c.dataset_id)).scalars():
        existing = LearnerRepository(session).get_dataset(str(identity))
        window = existing.holdout_window
        if (
            observation_identity(window.dataset) == observation_identity(dataset)
            and start <= window.end
            and window.start <= end
        ):
            raise ValueError("protected learner holdout cannot be recycled into a new window")


def register_dataset(database: Database, dataset: Dataset) -> str:
    checked = Dataset.model_validate_json(dataset.model_dump_json())
    with database.transaction() as session:
        guard_range(
            session,
            checked.train_window.dataset,
            min(r.features.input_start_at.date() for r in checked.rows),
            max(r.features.target_at.date() for r in checked.rows),
        )
        LearnerRepository(session).save_dataset(checked)
    return checked.identity


def _source_code_hash(root: Path) -> str:
    files = sorted((root / "src" / "quantbot" / "learner").glob("*.py"))
    return content_id({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files})


def train(
    database: Database,
    dataset_id: str,
    *,
    family: str = "ridge",
    alpha: float = 1.0,
    seed: int = 0,
    parent_model_id: str | None = None,
) -> ModelVersion:
    now = _utcnow()
    model_id = str(uuid4())
    with database.transaction() as session:
        repository = LearnerRepository(session)
        dataset = repository.get_dataset(dataset_id)
        guard_range(
            session,
            dataset.train_window.dataset,
            min(r.features.input_start_at.date() for r in dataset.rows),
            dataset.validation_window.end,
        )
        training = dataset.rows_for_fit(DataRole.DISCOVERY)
        validation = tuple(r for r in dataset.rows if r.role is DataRole.VALIDATION)
        if not training or not validation:
            raise ValueError("training and validation must both contain eligible observations")
        if any(r.features.target_at >= now for r in (*training, *validation)):
            raise ValueError("training and validation labels must already be observable")
        parent = repository.get_model(parent_model_id) if parent_model_id else None
        if parent is not None:
            parent_data = repository.get_dataset(parent.dataset_id)
            if (parent_data.source_id, parent_data.universe_id) != (
                dataset.source_id,
                dataset.universe_id,
            ):
                raise ValueError("challenger lineage must retain source and universe identity")
        # Spend the attempt before fitting. A failure is still a searched candidate. No future
        # protected labels are opened; this row joins the existing global search accounting.
        session.execute(
            search_runs.insert().values(
                search_id="learner:" + model_id,
                actor="elrond-learner",
                actor_version="1",
                candidates_evaluated=1,
                dataset=dataset.train_window.dataset,
                start_date=dataset.train_window.start.isoformat(),
                end_date=dataset.validation_window.end.isoformat(),
                data_role=DataRole.VALIDATION.value,
                started_at=encode_utc(now),
                finished_at=encode_utc(now),
                configuration_hash=content_id(
                    {"dataset": dataset_id, "family": family, "alpha": alpha, "seed": seed}
                ),
                detail_json=canonical_result_json(
                    {"phase": "attempt_committed_before_fit", "model_id": model_id}
                ),
            )
        )
    artifact = fit_estimator(
        tuple(r.features.values for r in training),
        tuple(r.excess_return for r in training),
        dataset.feature_names,
        family=family,
        alpha=alpha,
        seed=seed,
    )
    validation_scores = predict(artifact, tuple(r.features.values for r in validation))
    root = Path(__file__).resolve().parents[3]
    commit, dirty = capture_git_state(root)
    version = ModelVersion(
        model_id=model_id,
        dataset_id=dataset_id,
        created_at=_utcnow(),
        family=family,
        artifact=artifact,
        artifact_hash=content_id(artifact),
        feature_set=training[0].features.feature_set,
        horizon=training[0].features.horizon,
        training_start=min(r.features.as_of for r in training),
        training_end=max(r.features.target_at for r in training),
        validation_start=min(r.features.as_of for r in validation),
        validation_end=max(r.features.target_at for r in validation),
        training_observation_ids=tuple(r.identity for r in training),
        validation_observation_ids=tuple(r.identity for r in validation),
        validation_mse=mean(
            (score - row.excess_return) ** 2
            for score, row in zip(validation_scores, validation, strict=True)
        ),
        code_commit=commit,
        code_dirty=dirty,
        code_hash=_source_code_hash(root),
        environment=capture_environment(
            host_class="learner-research", lock_file=root / "uv.lock", random_seed=seed
        ),
        parent_model_id=parent_model_id,
        version=parent.version + 1 if parent else 1,
    )
    with database.transaction() as session:
        LearnerRepository(session).save_model(version)
    return version


def _issue(
    database: Database,
    model_id: str,
    features: Sequence[FeatureRow],
    evaluation_class: EvaluationClass,
) -> tuple[Prediction, ...]:
    now = _utcnow()
    checked = tuple(FeatureRow.model_validate_json(f.model_dump_json()) for f in features)
    if not checked:
        raise ValueError("no eligible feature rows")
    if len({f.as_of for f in checked}) != 1 or len({f.symbol for f in checked}) != len(checked):
        raise ValueError("predictions require one unique cross-section at a single session")
    with database.transaction() as session:
        repository = LearnerRepository(session)
        model = repository.get_model(model_id)
        dataset = repository.get_dataset(model.dataset_id)
        if tuple(sorted(f.symbol for f in checked)) != dataset.symbols:
            raise ValueError("prediction cohort must contain the complete declared universe")
        if evaluation_class is EvaluationClass.SHADOW:
            raise ValueError("LOCAL_DECLARED input cannot establish genuine shadow market evidence")
        for feature in checked:
            if (feature.source_id, feature.universe_id) != (dataset.source_id, dataset.universe_id):
                raise ValueError("prediction source and universe must match model lineage")
            if feature.as_of <= model.training_end:
                raise ValueError("prediction must be out of sample after training labels")
            if feature.as_of > now:
                raise ValueError("prediction features are not yet observable")
            if feature.benchmark_symbol != dataset.rows[0].features.benchmark_symbol:
                raise ValueError("prediction benchmark must match the training target")
            if evaluation_class is not EvaluationClass.HISTORICAL_REPLAY:
                if feature.as_of <= model.validation_end:
                    raise ValueError("shadow prediction must be unseen after validation labels")
                if feature.target_at <= now:
                    raise ValueError("shadow target is in the past; use historical replay")
                if now > feature.as_of + timedelta(minutes=5):
                    raise ValueError("prospective issuance deadline is five minutes after cutoff")
        # Every interval contains the same decision cutoff, so their union is this exact
        # range. Check it once, preserving all exclusions without decoding every dataset
        # again for every instrument in a broad cross-section.
        guard_range(
            session,
            dataset.train_window.dataset,
            min(feature.input_start_at.date() for feature in checked),
            max(feature.target_at.date() for feature in checked),
        )
        ordered = tuple(sorted(checked, key=lambda f: f.symbol))
        scores = predict(model.artifact, tuple(f.values for f in ordered))
        cohort = content_id(
            {
                "model": model_id,
                "features": [f.identity for f in ordered],
                "class": evaluation_class,
            }
        )
        result: list[Prediction] = []
        for feature, score in zip(ordered, scores, strict=True):
            prediction_id = content_id({"cohort": cohort, "symbol": feature.symbol})
            # A rerun returns the original durable time, never a rewritten claim.
            try:
                record = repository.get_prediction(prediction_id)
            except RecordNotFoundError:
                record = Prediction(
                    prediction_id=prediction_id,
                    model_id=model_id,
                    features=feature,
                    evaluation_class=evaluation_class,
                    created_at=now,
                    score=score,
                    rank=1 + sum(s > score for s in scores),
                    percentile=(
                        sum(s < score for s in scores) + sum(s == score for s in scores) / 2
                    )
                    / len(scores),
                    cohort_id=cohort,
                )
            repository.save_prediction(record)
            result.append(record)
    return tuple(result)


def issue_predictions(
    database: Database,
    model_id: str,
    features: Sequence[FeatureRow],
) -> tuple[Prediction, ...]:
    """Stamp with the service clock; callers cannot supply a claimed issuance timestamp."""
    return _issue(database, model_id, features, EvaluationClass.PROSPECTIVE_LOCAL)


def replay(database: Database, model_id: str) -> tuple[Prediction, ...]:
    with database.transaction() as session:
        repository = LearnerRepository(session)
        dataset = repository.get_dataset(repository.get_model(model_id).dataset_id)
    rows = tuple(r.features for r in dataset.rows if r.role is DataRole.VALIDATION)
    predictions: list[Prediction] = []
    for cutoff in sorted({row.as_of for row in rows}):
        predictions.extend(
            _issue(
                database,
                model_id,
                tuple(r for r in rows if r.as_of == cutoff),
                EvaluationClass.HISTORICAL_REPLAY,
            )
        )
    return tuple(predictions)


def resolve(
    database: Database,
    model_id: str,
    inputs: Sequence[DailyInput],
    *,
    source_id: str,
) -> tuple[Outcome, ...]:
    now = _utcnow()
    checked = tuple(DailyInput.model_validate_json(r.model_dump_json()) for r in inputs)
    indexed = {(r.bar.symbol, r.bar.timestamp): r for r in checked}
    if len(indexed) != len(checked):
        raise ValueError("duplicate outcome bars")
    result: list[Outcome] = []
    with database.transaction() as session:
        repository = LearnerRepository(session)
        dataset = repository.get_dataset(repository.get_model(model_id).dataset_id)
        predictions = repository.list_predictions(model_id)
        unresolved: list[Prediction] = []
        for prediction in predictions:
            feature = prediction.features
            if source_id != feature.source_id:
                raise ValueError("outcome source identity differs from prediction")
            if repository.get_outcome(prediction.prediction_id) is None:
                unresolved.append(prediction)
        for target_date in sorted({p.features.target_at.date() for p in unresolved}):
            guard_range(
                session,
                dataset.train_window.dataset,
                target_date,
                target_date,
            )
        for prediction in unresolved:
            feature = prediction.features
            target = indexed.get((feature.symbol, feature.target_at))
            benchmark = indexed.get((feature.benchmark_symbol, feature.target_at))
            if target is None or benchmark is None:
                continue
            available = max(target.available_at, benchmark.available_at)
            if feature.target_at > now or available > now:
                continue
            raw = float(target.bar.close) / feature.base_close - 1
            market = float(benchmark.bar.close) / feature.benchmark_close - 1
            outcome = Outcome(
                prediction_id=prediction.prediction_id,
                resolved_at=now,
                raw_return=raw,
                benchmark_return=market,
                excess_return=raw - market,
                error=raw - market - prediction.score,
                target_close=float(target.bar.close),
                benchmark_close=float(benchmark.bar.close),
                available_at=available,
                source_hash=content_id([target, benchmark]),
            )
            repository.save_outcome(outcome)
            result.append(outcome)
    return tuple(result)


def evaluate(database: Database, model_id: str, category: EvaluationClass) -> Evaluation:
    with database.transaction() as session:
        repository = LearnerRepository(session)
        repository.get_model(model_id)
        predictions = tuple(
            p for p in repository.list_predictions(model_id) if p.evaluation_class is category
        )
        scored: list[ScoredRow] = []
        for prediction in predictions:
            outcome = repository.get_outcome(prediction.prediction_id)
            if outcome is not None:
                scored.append(
                    ScoredRow(
                        prediction_id=prediction.prediction_id,
                        cohort_id=prediction.cohort_id,
                        symbol=prediction.features.symbol,
                        as_of=prediction.features.as_of,
                        target_at=prediction.features.target_at,
                        score=prediction.score,
                        raw_return=outcome.raw_return,
                        benchmark_return=outcome.benchmark_return,
                        excess_return=outcome.excess_return,
                    )
                )
        metrics = evaluate_predictions(scored, expected_count=len(predictions))
        metrics["evaluation_class"] = category.value
        evaluation = Evaluation(
            evaluation_id=str(uuid4()),
            model_id=model_id,
            evaluation_class=category,
            created_at=_utcnow(),
            metrics_json=canonical_result_json(metrics),
            prediction_ids=tuple(r.prediction_id for r in scored),
            expected_prediction_ids=tuple(p.prediction_id for p in predictions),
        )
        repository.save_evaluation(evaluation)
    return evaluation


def status(database: Database) -> dict[str, object]:
    with database.transaction() as session:
        repository = LearnerRepository(session)
        models = repository.list_models()
        predictions = repository.list_predictions()
        evaluations = repository.list_evaluations()
        return {
            "ok": True,
            "software_capability": "supervised_research_loop",
            "models": [
                {
                    "model_id": m.model_id,
                    "family": m.family,
                    "version": m.version,
                    "training_mse": m.artifact.training_mse,
                    "validation_mse": m.validation_mse,
                    "dataset_id": m.dataset_id,
                    "parent_model_id": m.parent_model_id,
                }
                for m in models
            ],
            "predictions": {
                c.value: sum(p.evaluation_class is c for p in predictions) for c in EvaluationClass
            },
            "evaluations": [
                {
                    "model_id": e.model_id,
                    "class": e.evaluation_class.value,
                    "metrics": json.loads(e.metrics_json),
                }
                for e in evaluations
            ],
            "protected_holdout_evidence": "NOT_EVALUATED",
            "source_evidence": "LOCAL_DECLARED; provider and historical membership unverified",
            "shadow_market_evidence": "NOT_ESTABLISHED",
            "forward_paper_evidence": "NOT_ESTABLISHED",
            "positive_expectancy_justified": False,
            "promotion": "NONE; existing research gates remain authoritative",
        }


def compare(database: Database, incumbent_id: str, challenger_id: str) -> dict[str, object]:
    """Compare matched realized forecasts. This cannot promote either model."""
    with database.transaction() as session:
        repository = LearnerRepository(session)
        incumbent, challenger = (
            repository.get_model(incumbent_id),
            repository.get_model(challenger_id),
        )
        if incumbent.model_id == challenger.model_id:
            raise ValueError("comparison requires distinct immutable model versions")
        if (incumbent.feature_set, incumbent.horizon) != (
            challenger.feature_set,
            challenger.horizon,
        ):
            raise ValueError("models have incompatible feature or horizon contracts")
        left = {
            p.features.identity: p
            for p in repository.list_predictions(incumbent_id)
            if p.evaluation_class is EvaluationClass.HISTORICAL_REPLAY
        }
        right = {
            p.features.identity: p
            for p in repository.list_predictions(challenger_id)
            if p.evaluation_class is EvaluationClass.HISTORICAL_REPLAY
        }
        paired: list[tuple[Prediction, Prediction, Outcome, Outcome]] = []
        for key in sorted(left.keys() & right.keys()):
            lo, ro = (
                repository.get_outcome(left[key].prediction_id),
                repository.get_outcome(right[key].prediction_id),
            )
            if lo is not None and ro is not None:
                if (lo.raw_return, lo.benchmark_return, lo.source_hash) != (
                    ro.raw_return,
                    ro.benchmark_return,
                    ro.source_hash,
                ):
                    raise ValueError("paired models resolved against different market outcomes")
                paired.append((left[key], right[key], lo, ro))
        if not paired:
            raise ValueError("comparison needs resolved predictions on a common validation sample")
        summaries: list[dict[str, object]] = []
        for prediction_index, outcome_index in ((0, 2), (1, 3)):
            scored: list[ScoredRow] = []
            for lp, rp, lo, ro in paired:
                p = lp if prediction_index == 0 else rp
                o = lo if outcome_index == 2 else ro
                scored.append(
                    ScoredRow(
                        prediction_id=p.prediction_id,
                        cohort_id=p.cohort_id,
                        symbol=p.features.symbol,
                        as_of=p.features.as_of,
                        target_at=p.features.target_at,
                        score=p.score,
                        raw_return=o.raw_return,
                        benchmark_return=o.benchmark_return,
                        excess_return=o.excess_return,
                    )
                )
            summaries.append(
                evaluate_predictions(scored, expected_count=max(len(left), len(right)))
            )
    return {
        "ok": True,
        "incumbent_id": incumbent_id,
        "challenger_id": challenger_id,
        "matched_observations": len(paired),
        "incumbent": summaries[0],
        "challenger": summaries[1],
        "promotion": "NONE",
        "evidence_class": "EXPLORATORY_MATCHED_REPLAY",
        "positive_expectancy_justified": False,
    }
