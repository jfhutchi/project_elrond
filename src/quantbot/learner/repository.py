"""Transaction-scoped immutable storage for supervised learner evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from statistics import mean
from typing import TYPE_CHECKING, TypeVar, cast

from pydantic import BaseModel, ValidationError
from sqlalchemy import Table, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from quantbot.research.manifest import canonical_result_json
from quantbot.research.registry import DataRole
from quantbot.storage import RecordNotFoundError, StateConflictError
from quantbot.storage.database import decode_utc, encode_utc
from quantbot.storage.schema import (
    learner_datasets,
    learner_evaluations,
    learner_model_versions,
    learner_outcomes,
    learner_predictions,
)

if TYPE_CHECKING:
    from quantbot.learner.data import Dataset
    from quantbot.learner.models import Evaluation, ModelVersion, Outcome, Prediction

ModelT = TypeVar("ModelT", bound=BaseModel)
_PENDING_PREDICTIONS = "learner_pending_prediction_ids"


def _utcnow() -> datetime:
    """Storage-owned clock used to prove when a prediction first became durable."""
    return datetime.now(UTC)


def _document_json(record: BaseModel) -> str:
    return canonical_result_json(record)


def _document_hash(document: str) -> str:
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def _row_matches(row: RowMapping, payload: Mapping[str, object]) -> bool:
    return all(row[key] == value for key, value in payload.items())


class LearnerRepository:
    """Persist learner records inside a caller-owned transaction without updating evidence."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._document_cache: dict[
            tuple[str, str], tuple[str, BaseModel, Mapping[str, object]]
        ] = {}
        self._model_dataset_checks: set[tuple[str, str]] = set()

    def _save_immutable(
        self,
        *,
        table: Table,
        identity_column: str,
        identity: str,
        payload: Mapping[str, object],
        kind: str,
    ) -> bool:
        row = (
            self._session.execute(
                select(table).where(table.c[identity_column] == identity)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            self._session.execute(table.insert().values(**payload))
            return True
        if _row_matches(row, payload):
            return False
        raise StateConflictError(f"{kind} {identity} conflicts with stored data")

    def _load_document(
        self,
        row: RowMapping,
        *,
        model_type: type[ModelT],
        payload: Callable[[ModelT], Mapping[str, object]],
        kind: str,
        identity: str,
    ) -> ModelT:
        raw_document = row["document_json"]
        if not isinstance(raw_document, str):
            raise StateConflictError(f"stored {kind} {identity} document is not text")
        if row["document_hash"] != _document_hash(raw_document):
            raise StateConflictError(f"stored {kind} {identity} document hash does not match")
        cache_key = (kind, identity)
        cached = self._document_cache.get(cache_key)
        if cached is not None and cached[0] == raw_document:
            if not _row_matches(row, cached[2]):
                raise StateConflictError(
                    f"stored {kind} {identity} promoted fields do not match its document"
                )
            return cast(ModelT, cached[1])
        try:
            record = model_type.model_validate_json(raw_document)
        except ValidationError as exc:
            raise StateConflictError(
                f"stored {kind} {identity} document does not satisfy its typed contract"
            ) from exc
        if raw_document != _document_json(record):
            raise StateConflictError(f"stored {kind} {identity} document is not canonical")
        expected_payload = payload(record)
        if not _row_matches(row, expected_payload):
            raise StateConflictError(
                f"stored {kind} {identity} promoted fields do not match its document"
            )
        self._document_cache[cache_key] = (raw_document, record, expected_payload)
        return record

    def _cached_document(self, kind: str, identity: str) -> str:
        cached = self._document_cache.get((kind, identity))
        if cached is None:
            raise RuntimeError(f"{kind} {identity} was not loaded before dependency validation")
        return cached[0]

    @staticmethod
    def _dataset_payload(dataset: Dataset) -> dict[str, object]:
        document = _document_json(dataset)
        return {
            "dataset_id": dataset.identity,
            "document_hash": _document_hash(document),
            "document_json": document,
        }

    def save_dataset(self, dataset: Dataset) -> bool:
        from quantbot.learner.data import Dataset

        validated = Dataset.model_validate(dataset.model_dump(mode="json"))
        payload = self._dataset_payload(validated)
        return self._save_immutable(
            table=learner_datasets,
            identity_column="dataset_id",
            identity=validated.identity,
            payload=payload,
            kind="learner dataset",
        )

    def get_dataset(self, dataset_id: str) -> Dataset:
        from quantbot.learner.data import Dataset

        row = (
            self._session.execute(
                select(learner_datasets).where(learner_datasets.c.dataset_id == dataset_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise RecordNotFoundError(f"learner dataset not found: {dataset_id}")
        return self._load_document(
            row,
            model_type=Dataset,
            payload=self._dataset_payload,
            kind="learner dataset",
            identity=dataset_id,
        )

    @staticmethod
    def _model_payload(model: ModelVersion) -> dict[str, object]:
        document = _document_json(model)
        return {
            "model_id": model.model_id,
            "dataset_id": model.dataset_id,
            "family": model.family,
            "created_at": encode_utc(model.created_at),
            "artifact_hash": model.artifact_hash,
            "document_hash": _document_hash(document),
            "document_json": document,
        }

    @staticmethod
    def _require_model_matches_dataset(
        model: ModelVersion, dataset: Dataset, *, verify_fit: bool = False
    ) -> None:
        from quantbot.learner.estimators import fit_estimator, predict

        training = tuple(
            row.identity for row in dataset.rows if row.role is DataRole.DISCOVERY
        )
        validation = tuple(
            row.identity for row in dataset.rows if row.role is DataRole.VALIDATION
        )
        if model.training_observation_ids != training:
            raise StateConflictError("model training observations do not match its stored dataset")
        if model.validation_observation_ids != validation:
            raise StateConflictError(
                "model validation observations do not match its stored dataset"
            )
        if model.artifact.feature_names != dataset.feature_names:
            raise StateConflictError("model artifact dimensions do not match its stored dataset")
        training_rows = tuple(
            row for row in dataset.rows if row.role is DataRole.DISCOVERY
        )
        validation_rows = tuple(
            row for row in dataset.rows if row.role is DataRole.VALIDATION
        )
        if not training_rows or not validation_rows:
            raise StateConflictError("model dataset must contain training and validation rows")
        if (
            model.training_start != min(row.features.as_of for row in training_rows)
            or model.training_end != max(row.features.target_at for row in training_rows)
            or model.validation_start
            != min(row.features.as_of for row in validation_rows)
            or model.validation_end
            != max(row.features.target_at for row in validation_rows)
        ):
            raise StateConflictError("model time bounds do not match its stored dataset")
        if any(
            row.features.feature_set != model.feature_set
            or row.features.horizon != model.horizon
            for row in dataset.rows
        ):
            raise StateConflictError("model feature contract does not match its stored dataset")
        if not verify_fit:
            return
        expected_artifact = fit_estimator(
            tuple(row.features.values for row in training_rows),
            tuple(row.excess_return for row in training_rows),
            dataset.feature_names,
            family=model.family,
            alpha=model.artifact.alpha,
            seed=model.artifact.seed,
        )
        if model.artifact != expected_artifact:
            raise StateConflictError("model artifact differs from the recomputed training fit")
        validation_scores = predict(
            expected_artifact,
            tuple(row.features.values for row in validation_rows),
        )
        validation_mse = mean(
            (score - row.excess_return) ** 2
            for score, row in zip(validation_scores, validation_rows, strict=True)
        )
        if model.validation_mse != validation_mse:
            raise StateConflictError("model validation MSE differs from stored validation rows")

    def save_model(self, model: ModelVersion) -> bool:
        from quantbot.learner.models import ModelVersion

        validated = ModelVersion.model_validate(model.model_dump(mode="json"))
        dataset = self.get_dataset(validated.dataset_id)
        self._require_model_matches_dataset(validated, dataset, verify_fit=True)
        if validated.parent_model_id is not None:
            try:
                parent = self.get_model(validated.parent_model_id)
            except RecordNotFoundError as exc:
                raise RecordNotFoundError(
                    f"learner parent model not found: {validated.parent_model_id}"
                ) from exc
            if validated.version != parent.version + 1:
                raise StateConflictError(
                    "successor model version does not immediately follow parent"
                )
            parent_dataset = self.get_dataset(parent.dataset_id)
            if (
                parent_dataset.source_id != dataset.source_id
                or parent_dataset.universe_id != dataset.universe_id
            ):
                raise StateConflictError("successor model lineage changed source or universe")
        payload = self._model_payload(validated)
        return self._save_immutable(
            table=learner_model_versions,
            identity_column="model_id",
            identity=validated.model_id,
            payload=payload,
            kind="learner model",
        )

    def get_model(self, model_id: str) -> ModelVersion:
        from quantbot.learner.models import ModelVersion

        row = (
            self._session.execute(
                select(learner_model_versions).where(
                    learner_model_versions.c.model_id == model_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise RecordNotFoundError(f"learner model not found: {model_id}")
        model = self._load_document(
            row,
            model_type=ModelVersion,
            payload=self._model_payload,
            kind="learner model",
            identity=model_id,
        )
        dataset = self.get_dataset(model.dataset_id)
        dependency_key = (
            self._cached_document("learner model", model_id),
            self._cached_document("learner dataset", model.dataset_id),
        )
        if dependency_key not in self._model_dataset_checks:
            self._require_model_matches_dataset(model, dataset)
            self._model_dataset_checks.add(dependency_key)
        return model

    def list_models(self) -> tuple[ModelVersion, ...]:
        rows = self._session.execute(
            select(learner_model_versions).order_by(
                learner_model_versions.c.created_at,
                learner_model_versions.c.model_id,
            )
        ).mappings()
        return tuple(self.get_model(str(row["model_id"])) for row in rows)

    @staticmethod
    def _prediction_payload(
        prediction: Prediction, *, recorded_at: datetime | None = None
    ) -> dict[str, object]:
        document = _document_json(prediction)
        payload: dict[str, object] = {
            "prediction_id": prediction.prediction_id,
            "model_id": prediction.model_id,
            "evaluation_class": prediction.evaluation_class.value,
            "created_at": encode_utc(prediction.created_at),
            "cohort_id": prediction.cohort_id,
            "document_hash": _document_hash(document),
            "document_json": document,
        }
        if recorded_at is not None:
            payload["recorded_at"] = encode_utc(recorded_at)
        return payload

    @staticmethod
    def _require_prediction_matches_model(
        prediction: Prediction,
        model: ModelVersion,
        dataset: Dataset,
        *,
        recorded_at: datetime | None = None,
    ) -> None:
        from datetime import timedelta

        from quantbot.learner.estimators import predict

        features = prediction.features
        if model.training_end >= features.as_of:
            raise StateConflictError("prediction features do not follow the model training window")
        if features.feature_set != model.feature_set or features.horizon != model.horizon:
            raise StateConflictError("prediction feature contract does not match its model")
        if (
            features.source_id != dataset.source_id
            or features.universe_id != dataset.universe_id
            or features.symbol not in dataset.symbols
        ):
            raise StateConflictError("prediction lineage does not match its model dataset")
        expected_score = predict(model.artifact, (features.values,))[0]
        if prediction.score != expected_score:
            raise StateConflictError("prediction score does not match its stored model artifact")
        prospective = prediction.evaluation_class.value in {
            "SHADOW",
            "PROSPECTIVE_LOCAL",
        }
        if (
            prediction.evaluation_class.value == "SHADOW"
            and dataset.source_evidence == "LOCAL_DECLARED"
        ):
            raise StateConflictError("SHADOW evidence requires a trusted source producer")
        if prospective and recorded_at is not None:
            if recorded_at < model.created_at:
                raise StateConflictError("prospective prediction was recorded before its model")
            if recorded_at > features.as_of + timedelta(minutes=5):
                raise StateConflictError("prospective prediction missed its five-minute deadline")
            if recorded_at >= features.target_at:
                raise StateConflictError("prospective prediction target is already knowable")

    def save_prediction(self, prediction: Prediction) -> bool:
        from quantbot.learner.models import Prediction

        validated = Prediction.model_validate(prediction.model_dump(mode="json"))
        model = self.get_model(validated.model_id)
        dataset = self.get_dataset(model.dataset_id)
        existing = (
            self._session.execute(
                select(learner_predictions).where(
                    learner_predictions.c.prediction_id == validated.prediction_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            recorded_at = decode_utc(str(existing["recorded_at"]))
            self._require_prediction_matches_model(
                validated, model, dataset, recorded_at=recorded_at
            )
            payload = self._prediction_payload(validated, recorded_at=recorded_at)
            if _row_matches(existing, payload):
                return False
            raise StateConflictError(
                f"learner prediction {validated.prediction_id} conflicts with stored data"
            )

        recorded_at = _utcnow()
        self._require_prediction_matches_model(
            validated, model, dataset, recorded_at=recorded_at
        )
        self._session.execute(
            learner_predictions.insert().values(
                **self._prediction_payload(validated, recorded_at=recorded_at)
            )
        )
        pending = self._session.info.setdefault(_PENDING_PREDICTIONS, set())
        assert isinstance(pending, set)
        pending.add(validated.prediction_id)
        return True

    def get_prediction(self, prediction_id: str) -> Prediction:
        from quantbot.learner.models import Prediction

        row = (
            self._session.execute(
                select(learner_predictions).where(
                    learner_predictions.c.prediction_id == prediction_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise RecordNotFoundError(f"learner prediction not found: {prediction_id}")
        prediction = self._load_document(
            row,
            model_type=Prediction,
            payload=self._prediction_payload,
            kind="learner prediction",
            identity=prediction_id,
        )
        try:
            recorded_at = decode_utc(str(row["recorded_at"]))
        except (TypeError, ValueError) as exc:
            raise StateConflictError(
                f"stored learner prediction {prediction_id} has an invalid recorded_at"
            ) from exc
        model = self.get_model(prediction.model_id)
        self._require_prediction_matches_model(
            prediction,
            model,
            self.get_dataset(model.dataset_id),
            recorded_at=recorded_at,
        )
        return prediction

    def list_predictions(self, model_id: str | None = None) -> tuple[Prediction, ...]:
        statement = select(learner_predictions.c.prediction_id)
        if model_id is not None:
            statement = statement.where(learner_predictions.c.model_id == model_id)
        ids = self._session.execute(
            statement.order_by(
                learner_predictions.c.recorded_at,
                learner_predictions.c.prediction_id,
            )
        ).scalars()
        return tuple(self.get_prediction(str(prediction_id)) for prediction_id in ids)

    @staticmethod
    def _outcome_payload(outcome: Outcome) -> dict[str, object]:
        document = _document_json(outcome)
        return {
            "prediction_id": outcome.prediction_id,
            "resolved_at": encode_utc(outcome.resolved_at),
            "source_hash": outcome.source_hash,
            "document_hash": _document_hash(document),
            "document_json": document,
        }

    @staticmethod
    def _require_outcome_matches_prediction(outcome: Outcome, prediction: Prediction) -> None:
        features = prediction.features
        if outcome.available_at < features.target_at:
            raise StateConflictError("outcome source was not available at its target")
        if outcome.resolved_at < prediction.created_at:
            raise StateConflictError("outcome resolution predates its prediction")
        raw_return = outcome.target_close / features.base_close - 1.0
        benchmark_return = outcome.benchmark_close / features.benchmark_close - 1.0
        excess_return = raw_return - benchmark_return
        if (
            outcome.raw_return != raw_return
            or outcome.benchmark_return != benchmark_return
            or outcome.excess_return != excess_return
            or outcome.error != excess_return - prediction.score
        ):
            raise StateConflictError("outcome derived returns do not match its frozen prediction")

    def save_outcome(self, outcome: Outcome) -> bool:
        from quantbot.learner.models import Outcome

        validated = Outcome.model_validate(outcome.model_dump(mode="json"))
        pending = self._session.info.get(_PENDING_PREDICTIONS, set())
        if validated.prediction_id in pending:
            raise StateConflictError(
                "outcome requires a prediction committed by a prior durable transaction"
            )
        now = _utcnow()
        if validated.available_at > now or validated.resolved_at > now:
            raise StateConflictError("outcome source or resolution time is still in the future")
        prediction = self.get_prediction(validated.prediction_id)
        self._require_outcome_matches_prediction(validated, prediction)
        payload = self._outcome_payload(validated)
        return self._save_immutable(
            table=learner_outcomes,
            identity_column="prediction_id",
            identity=validated.prediction_id,
            payload=payload,
            kind="learner outcome",
        )

    def get_outcome(self, prediction_id: str) -> Outcome | None:
        from quantbot.learner.models import Outcome

        row = (
            self._session.execute(
                select(learner_outcomes).where(
                    learner_outcomes.c.prediction_id == prediction_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        outcome = self._load_document(
            row,
            model_type=Outcome,
            payload=self._outcome_payload,
            kind="learner outcome",
            identity=prediction_id,
        )
        prediction = self.get_prediction(outcome.prediction_id)
        self._require_outcome_matches_prediction(outcome, prediction)
        return outcome

    @staticmethod
    def _evaluation_payload(evaluation: Evaluation) -> dict[str, object]:
        document = _document_json(evaluation)
        return {
            "evaluation_id": evaluation.evaluation_id,
            "model_id": evaluation.model_id,
            "evaluation_class": evaluation.evaluation_class.value,
            "created_at": encode_utc(evaluation.created_at),
            "document_hash": _document_hash(document),
            "document_json": document,
        }

    def _require_evaluation_inputs(
        self, evaluation: Evaluation, *, require_complete_cohort: bool = False
    ) -> None:
        from quantbot.learner.metrics import ScoredRow, evaluate_predictions

        self.get_model(evaluation.model_id)
        try:
            metrics = json.loads(evaluation.metrics_json)
        except json.JSONDecodeError as exc:
            raise StateConflictError("evaluation metrics_json is not valid JSON") from exc
        if (
            not isinstance(metrics, dict)
            or canonical_result_json(metrics) != evaluation.metrics_json
        ):
            raise StateConflictError("evaluation metrics_json must be a canonical JSON object")

        expected_predictions = tuple(
            self.get_prediction(prediction_id)
            for prediction_id in evaluation.expected_prediction_ids
        )
        for prediction in expected_predictions:
            if prediction.model_id != evaluation.model_id:
                raise StateConflictError("evaluation prediction belongs to a different model")
            if prediction.evaluation_class is not evaluation.evaluation_class:
                raise StateConflictError("evaluation prediction has a different evidence class")
        if require_complete_cohort:
            stored_ids = {
                prediction.prediction_id
                for prediction in self.list_predictions(evaluation.model_id)
                if prediction.evaluation_class is evaluation.evaluation_class
            }
            if set(evaluation.expected_prediction_ids) != stored_ids:
                raise StateConflictError(
                    "evaluation expected predictions do not match the stored evidence cohort"
                )

        predictions_by_id = {
            prediction.prediction_id: prediction for prediction in expected_predictions
        }
        scored: list[ScoredRow] = []
        for prediction_id in evaluation.prediction_ids:
            prediction = predictions_by_id[prediction_id]
            outcome = self.get_outcome(prediction_id)
            if outcome is None:
                raise StateConflictError("evaluation prediction has no resolved outcome")
            if evaluation.created_at < outcome.resolved_at:
                raise StateConflictError("evaluation predates one of its resolved outcomes")
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
        try:
            expected_metrics = evaluate_predictions(
                scored, expected_count=len(expected_predictions)
            )
        except ValueError as exc:
            raise StateConflictError("evaluation inputs cannot produce valid metrics") from exc
        expected_metrics["evaluation_class"] = evaluation.evaluation_class.value
        if evaluation.metrics_json != canonical_result_json(expected_metrics):
            raise StateConflictError(
                "evaluation metrics do not match its stored predictions and outcomes"
            )

    def save_evaluation(self, evaluation: Evaluation) -> bool:
        from quantbot.learner.models import Evaluation

        validated = Evaluation.model_validate(evaluation.model_dump(mode="json"))
        self._require_evaluation_inputs(validated, require_complete_cohort=True)
        payload = self._evaluation_payload(validated)
        return self._save_immutable(
            table=learner_evaluations,
            identity_column="evaluation_id",
            identity=validated.evaluation_id,
            payload=payload,
            kind="learner evaluation",
        )

    def get_evaluation(self, evaluation_id: str) -> Evaluation:
        from quantbot.learner.models import Evaluation

        row = (
            self._session.execute(
                select(learner_evaluations).where(
                    learner_evaluations.c.evaluation_id == evaluation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise RecordNotFoundError(f"learner evaluation not found: {evaluation_id}")
        evaluation = self._load_document(
            row,
            model_type=Evaluation,
            payload=self._evaluation_payload,
            kind="learner evaluation",
            identity=evaluation_id,
        )
        self._require_evaluation_inputs(evaluation)
        return evaluation

    def list_evaluations(self, model_id: str | None = None) -> tuple[Evaluation, ...]:
        statement = select(learner_evaluations.c.evaluation_id)
        if model_id is not None:
            statement = statement.where(learner_evaluations.c.model_id == model_id)
        ids = self._session.execute(
            statement.order_by(
                learner_evaluations.c.created_at,
                learner_evaluations.c.evaluation_id,
            )
        ).scalars()
        return tuple(self.get_evaluation(str(evaluation_id)) for evaluation_id in ids)


__all__ = ["LearnerRepository"]
