"""Persistence contracts for immutable learner artifacts and observations."""

from datetime import UTC, date, datetime
from pathlib import Path
from statistics import mean

import pytest
from sqlalchemy import inspect, select, update

from quantbot.learner.data import FEATURE_NAMES, Dataset, FeatureRow, LabelRow
from quantbot.learner.estimators import fit_estimator, predict
from quantbot.learner.metrics import ScoredRow, evaluate_predictions
from quantbot.learner.models import (
    Evaluation,
    EvaluationClass,
    ModelVersion,
    Outcome,
    Prediction,
)
from quantbot.learner.repository import LearnerRepository
from quantbot.research.manifest import EnvironmentFingerprint, canonical_result_json, content_id
from quantbot.research.registry import DataRole, DataWindow
from quantbot.storage import Database, RecordNotFoundError, StateConflictError
from quantbot.storage.schema import learner_datasets
from quantbot.storage.schema import learner_predictions as prediction_table


def _feature(
    *,
    symbol: str,
    as_of: datetime,
    target_at: datetime,
    feature_set: str = "daily-alpha-v1",
    horizon: int = 5,
) -> FeatureRow:
    return FeatureRow(
        input_start_at=datetime(2025, 11, 1, 21, tzinfo=UTC),
        as_of=as_of,
        target_at=target_at,
        asof_session_index=100,
        target_session_index=100 + horizon,
        calendar_hash="f" * 64,
        symbol=symbol,
        universe_id="us-liquid-v1",
        source_id="fixture-v1",
        feature_set=feature_set,
        horizon=horizon,
        base_close=100.0,
        benchmark_close=100.0,
        values=tuple(float(index) for index in range(len(FEATURE_NAMES))),
        source_hash="a" * 64,
    )


def _dataset() -> Dataset:
    train_feature = _feature(
        symbol="AAPL",
        as_of=datetime(2026, 1, 2, 21, tzinfo=UTC),
        target_at=datetime(2026, 1, 5, 21, tzinfo=UTC),
    )
    validation_feature = _feature(
        symbol="MSFT",
        as_of=datetime(2026, 1, 12, 21, tzinfo=UTC),
        target_at=datetime(2026, 1, 13, 21, tzinfo=UTC),
    )
    return Dataset(
        rows=(
            LabelRow(
                features=train_feature,
                role=DataRole.DISCOVERY,
                raw_return=0.02,
                benchmark_return=0.01,
                excess_return=0.01,
                target_source_hash="1" * 64,
            ),
            LabelRow(
                features=validation_feature,
                role=DataRole.VALIDATION,
                raw_return=-0.01,
                benchmark_return=0.01,
                excess_return=-0.02,
                target_source_hash="2" * 64,
            ),
        ),
        train_window=DataWindow(
            dataset="bars-v1",
            snapshot="snapshot-v1",
            role=DataRole.DISCOVERY,
            start=date(2026, 1, 1),
            end=date(2026, 1, 9),
        ),
        validation_window=DataWindow(
            dataset="bars-v1",
            snapshot="snapshot-v1",
            role=DataRole.VALIDATION,
            start=date(2026, 1, 10),
            end=date(2026, 1, 16),
        ),
        holdout_window=DataWindow(
            dataset="bars-v1",
            snapshot="snapshot-v1",
            role=DataRole.PROTECTED_EVALUATION,
            start=date(2026, 1, 17),
            end=date(2026, 1, 30),
        ),
        universe_id="us-liquid-v1",
        source_id="fixture-v1",
        symbols=("AAPL", "MSFT"),
    )


def _model(dataset: Dataset) -> ModelVersion:
    training = dataset.rows_for_fit(DataRole.DISCOVERY)
    validation = tuple(row for row in dataset.rows if row.role is DataRole.VALIDATION)
    artifact = fit_estimator(
        tuple(row.features.values for row in training),
        tuple(row.excess_return for row in training),
        FEATURE_NAMES,
        family="ridge",
        alpha=1.0,
        seed=7,
    )
    validation_scores = predict(
        artifact, tuple(row.features.values for row in validation)
    )
    validation_mse = mean(
        (score - row.excess_return) ** 2
        for score, row in zip(validation_scores, validation, strict=True)
    )
    return ModelVersion(
        model_id="model-v1",
        dataset_id=dataset.identity,
        created_at=datetime(2026, 1, 20, tzinfo=UTC),
        family="ridge",
        artifact=artifact,
        artifact_hash=content_id(artifact),
        feature_set="daily-alpha-v1",
        horizon=5,
        training_start=training[0].features.as_of,
        training_end=training[-1].features.target_at,
        validation_start=validation[0].features.as_of,
        validation_end=validation[-1].features.target_at,
        training_observation_ids=tuple(row.identity for row in training),
        validation_observation_ids=tuple(row.identity for row in validation),
        validation_mse=validation_mse,
        code_commit="b" * 40,
        code_dirty=False,
        code_hash="c" * 64,
        environment=EnvironmentFingerprint(
            python_version="3.11.9",
            platform="test",
            dependency_lock_hash="d" * 64,
            host_class="ci",
            random_seed=7,
        ),
    )


def _prediction(model: ModelVersion) -> Prediction:
    return Prediction(
        prediction_id="prediction-v1",
        model_id=model.model_id,
        features=_feature(
            symbol="AAPL",
            as_of=datetime(2026, 2, 2, 21, tzinfo=UTC),
            target_at=datetime(2026, 2, 9, 21, tzinfo=UTC),
        ),
        evaluation_class=EvaluationClass.PROSPECTIVE_LOCAL,
        created_at=datetime(2026, 2, 2, 21, 1, tzinfo=UTC),
        score=0.01,
        rank=1,
        percentile=1.0,
        cohort_id="2026-02-02",
    )


def _outcome(prediction: Prediction) -> Outcome:
    target_close = 105.0
    benchmark_close = 102.0
    raw_return = target_close / prediction.features.base_close - 1.0
    benchmark_return = benchmark_close / prediction.features.benchmark_close - 1.0
    excess_return = raw_return - benchmark_return
    return Outcome(
        prediction_id=prediction.prediction_id,
        resolved_at=datetime(2026, 2, 9, 22, tzinfo=UTC),
        raw_return=raw_return,
        benchmark_return=benchmark_return,
        excess_return=excess_return,
        error=excess_return - prediction.score,
        source_hash="e" * 64,
        target_close=target_close,
        benchmark_close=benchmark_close,
        available_at=datetime(2026, 2, 9, 21, 5, tzinfo=UTC),
    )


def _evaluation(prediction: Prediction, outcome: Outcome) -> Evaluation:
    metrics = evaluate_predictions(
        (
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
            ),
        ),
        expected_count=1,
    )
    metrics["evaluation_class"] = prediction.evaluation_class.value
    return Evaluation(
        evaluation_id="evaluation-v1",
        model_id=prediction.model_id,
        evaluation_class=prediction.evaluation_class,
        created_at=outcome.resolved_at,
        metrics_json=canonical_result_json(metrics),
        prediction_ids=(prediction.prediction_id,),
        expected_prediction_ids=(prediction.prediction_id,),
    )


def test_new_database_contains_the_learner_evidence_tables(tmp_path: Path) -> None:
    """Removing the v29 migration must make learner evidence impossible to persist."""
    path = tmp_path / "learner.db"

    with Database.create(path) as database:
        table_names = set(inspect(database.engine).get_table_names())

    assert {
        "learner_datasets",
        "learner_model_versions",
        "learner_predictions",
        "learner_outcomes",
        "learner_evaluations",
    } <= table_names


def test_repository_round_trips_each_immutable_record_and_retries_idempotently(
    tmp_path: Path,
) -> None:
    dataset = _dataset()
    model = _model(dataset)
    prediction = _prediction(model).model_copy(
        update={"evaluation_class": EvaluationClass.HISTORICAL_REPLAY}
    )
    outcome = _outcome(prediction)
    evaluation = _evaluation(prediction, outcome)

    with Database.create(tmp_path / "learner.db") as database:
        with database.transaction() as session:
            repository = LearnerRepository(session)
            assert repository.save_dataset(dataset)
            assert repository.save_model(model)
            assert repository.save_prediction(prediction)
            assert not repository.save_dataset(dataset)
            assert not repository.save_model(model)
            assert not repository.save_prediction(prediction)
        with database.transaction() as session:
            repository = LearnerRepository(session)
            assert repository.save_outcome(outcome)
            assert repository.save_evaluation(evaluation)
            assert not repository.save_outcome(outcome)
            assert not repository.save_evaluation(evaluation)
            assert repository.get_dataset(dataset.identity) == dataset
            assert repository.get_model(model.model_id) == model
            assert repository.list_models() == (model,)
            assert repository.get_prediction(prediction.prediction_id) == prediction
            assert repository.list_predictions(model_id=model.model_id) == (prediction,)
            assert repository.get_outcome(prediction.prediction_id) == outcome
            assert repository.get_evaluation(evaluation.evaluation_id) == evaluation
            assert repository.list_evaluations(model_id=model.model_id) == (evaluation,)


def test_shadow_prediction_uses_an_internal_recorded_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = _dataset()
    model = _model(dataset)
    prediction = _prediction(model)
    recorded_at = datetime(2026, 2, 2, 21, 2, tzinfo=UTC)
    monkeypatch.setattr(
        "quantbot.learner.repository._utcnow", lambda: recorded_at, raising=False
    )

    with Database.create(tmp_path / "learner.db") as database:
        with database.transaction() as session:
            repository = LearnerRepository(session)
            repository.save_dataset(dataset)
            repository.save_model(model)
            repository.save_prediction(prediction)
            stored = session.execute(
                select(prediction_table.c.recorded_at).where(
                    prediction_table.c.prediction_id == prediction.prediction_id
                )
            ).scalar_one()

    assert stored == "2026-02-02T21:02:00Z"


def test_late_prospective_prediction_is_refused_after_its_deadline(tmp_path: Path) -> None:
    dataset = _dataset()
    model = _model(dataset)
    prediction = _prediction(model)
    with Database.create(tmp_path / "learner.db") as database:
        with database.transaction() as session:
            repository = LearnerRepository(session)
            repository.save_dataset(dataset)
            repository.save_model(model)
            with pytest.raises(StateConflictError, match="five-minute deadline"):
                repository.save_prediction(prediction)


def test_outcome_requires_a_prediction_committed_by_an_earlier_session(
    tmp_path: Path,
) -> None:
    dataset = _dataset()
    model = _model(dataset)
    prediction = _prediction(model).model_copy(
        update={"evaluation_class": EvaluationClass.HISTORICAL_REPLAY}
    )
    outcome = _outcome(prediction)
    with Database.create(tmp_path / "learner.db") as database:
        with database.transaction() as session:
            repository = LearnerRepository(session)
            repository.save_dataset(dataset)
            repository.save_model(model)
            repository.save_prediction(prediction)
            with pytest.raises(StateConflictError, match="prior durable transaction"):
                repository.save_outcome(outcome)


def test_outcome_cannot_be_prewritten_before_its_source_is_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = _dataset()
    model = _model(dataset)
    prediction = _prediction(model).model_copy(
        update={"evaluation_class": EvaluationClass.HISTORICAL_REPLAY}
    )
    outcome = _outcome(prediction)
    monkeypatch.setattr(
        "quantbot.learner.repository._utcnow",
        lambda: datetime(2026, 2, 2, 22, tzinfo=UTC),
    )
    with Database.create(tmp_path / "learner.db") as database:
        with database.transaction() as session:
            repository = LearnerRepository(session)
            repository.save_dataset(dataset)
            repository.save_model(model)
            repository.save_prediction(prediction)
        monkeypatch.setattr(
            "quantbot.learner.repository._utcnow",
            lambda: datetime(2026, 2, 8, 22, tzinfo=UTC),
        )
        with database.transaction() as session:
            with pytest.raises(StateConflictError, match="still in the future"):
                LearnerRepository(session).save_outcome(outcome)


def test_dataset_read_refuses_a_document_changed_behind_promoted_columns(tmp_path: Path) -> None:
    dataset = _dataset()
    with Database.create(tmp_path / "learner.db") as database:
        with database.transaction() as session:
            repository = LearnerRepository(session)
            repository.save_dataset(dataset)
            assert repository.get_dataset(dataset.identity) == dataset
            session.execute(
                update(learner_datasets)
                .where(learner_datasets.c.dataset_id == dataset.identity)
                .values(document_json='{"changed":true}')
            )
            with pytest.raises(StateConflictError, match="document"):
                repository.get_dataset(dataset.identity)


def test_cached_dataset_read_still_checks_the_current_stored_hash(tmp_path: Path) -> None:
    dataset = _dataset()
    with Database.create(tmp_path / "learner.db") as database:
        with database.transaction() as session:
            repository = LearnerRepository(session)
            repository.save_dataset(dataset)
            assert repository.get_dataset(dataset.identity) == dataset
            session.execute(
                update(learner_datasets)
                .where(learner_datasets.c.dataset_id == dataset.identity)
                .values(document_hash="0" * 64)
            )
            with pytest.raises(StateConflictError, match="document hash"):
                repository.get_dataset(dataset.identity)


def test_unchanged_model_and_dataset_lineage_is_derived_once_per_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = _dataset()
    model = _model(dataset)
    with Database.create(tmp_path / "learner.db") as database:
        with database.transaction() as session:
            repository = LearnerRepository(session)
            repository.save_dataset(dataset)
            repository.save_model(model)
            original = repository._require_model_matches_dataset
            checks = 0

            def counted_check(
                checked_model: ModelVersion,
                checked_dataset: Dataset,
                *,
                verify_fit: bool = False,
            ) -> None:
                nonlocal checks
                checks += 1
                original(checked_model, checked_dataset, verify_fit=verify_fit)

            monkeypatch.setattr(repository, "_require_model_matches_dataset", counted_check)
            assert repository.get_model(model.model_id) == model
            assert repository.get_model(model.model_id) == model

    assert checks == 1


def test_model_must_name_exact_discovery_and_validation_rows(tmp_path: Path) -> None:
    dataset = _dataset()
    forged = _model(dataset).model_copy(update={"training_observation_ids": ("forged",)})
    with Database.create(tmp_path / "learner.db") as database:
        with database.transaction() as session:
            repository = LearnerRepository(session)
            repository.save_dataset(dataset)
            with pytest.raises(StateConflictError, match="training observations"):
                repository.save_model(forged)


def test_successor_model_requires_a_stored_immediate_parent(tmp_path: Path) -> None:
    dataset = _dataset()
    successor = _model(dataset).model_copy(
        update={"model_id": "model-v2", "parent_model_id": "missing", "version": 2}
    )
    with Database.create(tmp_path / "learner.db") as database:
        with database.transaction() as session:
            repository = LearnerRepository(session)
            repository.save_dataset(dataset)
            with pytest.raises(RecordNotFoundError, match="parent model"):
                repository.save_model(successor)


def test_model_artifact_is_recomputed_from_its_stored_training_rows(tmp_path: Path) -> None:
    dataset = _dataset()
    model = _model(dataset)
    changed_artifact = model.artifact.model_copy(update={"intercept": 99.0})
    forged = model.model_copy(
        update={"artifact": changed_artifact, "artifact_hash": content_id(changed_artifact)}
    )
    with Database.create(tmp_path / "learner.db") as database:
        with database.transaction() as session:
            repository = LearnerRepository(session)
            repository.save_dataset(dataset)
            with pytest.raises(StateConflictError, match="recomputed training fit"):
                repository.save_model(forged)


def test_prediction_requires_a_stored_model_and_post_training_features(tmp_path: Path) -> None:
    dataset = _dataset()
    model = _model(dataset)
    prediction = _prediction(model)
    pretraining = prediction.model_copy(
        update={
            "features": _feature(
                symbol="AAPL",
                as_of=datetime(2026, 1, 3, 21, tzinfo=UTC),
                target_at=datetime(2026, 1, 9, 21, tzinfo=UTC),
            ),
            "evaluation_class": EvaluationClass.HISTORICAL_REPLAY,
            "created_at": datetime(2026, 1, 20, 1, tzinfo=UTC),
        }
    )
    with Database.create(tmp_path / "learner.db") as database:
        with database.transaction() as session:
            repository = LearnerRepository(session)
            with pytest.raises(RecordNotFoundError, match="model"):
                repository.save_prediction(prediction)
            repository.save_dataset(dataset)
            repository.save_model(model)
            with pytest.raises(StateConflictError, match="training window"):
                repository.save_prediction(pretraining)


def test_prediction_score_must_come_from_the_stored_model_artifact(tmp_path: Path) -> None:
    dataset = _dataset()
    model = _model(dataset)
    forged = _prediction(model).model_copy(
        update={
            "evaluation_class": EvaluationClass.HISTORICAL_REPLAY,
            "score": 123.0,
        }
    )
    with Database.create(tmp_path / "learner.db") as database:
        with database.transaction() as session:
            repository = LearnerRepository(session)
            repository.save_dataset(dataset)
            repository.save_model(model)
            with pytest.raises(StateConflictError, match="score"):
                repository.save_prediction(forged)


def test_outcome_is_recomputed_against_the_frozen_prediction(tmp_path: Path) -> None:
    dataset = _dataset()
    model = _model(dataset)
    prediction = _prediction(model).model_copy(
        update={"evaluation_class": EvaluationClass.HISTORICAL_REPLAY}
    )
    outcome = _outcome(prediction)
    wrong_raw = outcome.raw_return + 0.1
    forged = outcome.model_copy(
        update={
            "raw_return": wrong_raw,
            "excess_return": wrong_raw - outcome.benchmark_return,
            "error": wrong_raw - outcome.benchmark_return - prediction.score,
        }
    )
    with Database.create(tmp_path / "learner.db") as database:
        with database.transaction() as session:
            repository = LearnerRepository(session)
            repository.save_dataset(dataset)
            repository.save_model(model)
            repository.save_prediction(prediction)
        with database.transaction() as session:
            repository = LearnerRepository(session)
            with pytest.raises(StateConflictError, match="derived returns"):
                repository.save_outcome(forged)


def test_evaluation_metrics_are_recomputed_from_stored_evidence(tmp_path: Path) -> None:
    dataset = _dataset()
    model = _model(dataset)
    prediction = _prediction(model).model_copy(
        update={"evaluation_class": EvaluationClass.HISTORICAL_REPLAY}
    )
    outcome = _outcome(prediction)
    forged = _evaluation(prediction, outcome).model_copy(
        update={"metrics_json": canonical_result_json({"sample_count": 999})}
    )
    with Database.create(tmp_path / "learner.db") as database:
        with database.transaction() as session:
            repository = LearnerRepository(session)
            repository.save_dataset(dataset)
            repository.save_model(model)
            repository.save_prediction(prediction)
        with database.transaction() as session:
            repository = LearnerRepository(session)
            repository.save_outcome(outcome)
            with pytest.raises(StateConflictError, match="metrics"):
                repository.save_evaluation(forged)


def test_evaluation_denominator_is_the_complete_stored_category(tmp_path: Path) -> None:
    dataset = _dataset()
    model = _model(dataset)
    prediction = _prediction(model).model_copy(
        update={"evaluation_class": EvaluationClass.HISTORICAL_REPLAY}
    )
    second = prediction.model_copy(update={"prediction_id": "prediction-v2"})
    outcome = _outcome(prediction)
    evaluation = _evaluation(prediction, outcome)
    with Database.create(tmp_path / "learner.db") as database:
        with database.transaction() as session:
            repository = LearnerRepository(session)
            repository.save_dataset(dataset)
            repository.save_model(model)
            repository.save_prediction(prediction)
            repository.save_prediction(second)
        with database.transaction() as session:
            repository = LearnerRepository(session)
            repository.save_outcome(outcome)
            with pytest.raises(StateConflictError, match="stored evidence cohort"):
                repository.save_evaluation(evaluation)
