"""Immutable learner lineage; predictions carry no trading or promotion authority."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quantbot.learner.data import FeatureRow
from quantbot.learner.estimators import FittedEstimator
from quantbot.research.manifest import EnvironmentFingerprint, content_id

Finite = Annotated[float, Field(allow_inf_nan=False)]
Text = Annotated[str, Field(min_length=1)]


class LearnerModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    @field_validator("*", mode="after")
    @classmethod
    def aware_timestamp(cls, value: object) -> object:
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("timestamp must be timezone-aware")
            return value.astimezone(UTC)
        return value


class EvaluationClass(StrEnum):
    HISTORICAL_REPLAY = "HISTORICAL_REPLAY"
    PROSPECTIVE_LOCAL = "PROSPECTIVE_LOCAL"
    SHADOW = "SHADOW"


class ModelVersion(LearnerModel):
    model_id: Text
    dataset_id: Text
    created_at: datetime
    family: Text
    artifact: FittedEstimator
    artifact_hash: Text
    feature_set: Text
    horizon: int = Field(default=5, ge=1)
    training_start: datetime
    training_end: datetime
    validation_start: datetime
    validation_end: datetime
    training_observation_ids: tuple[str, ...] = Field(min_length=1)
    validation_observation_ids: tuple[str, ...] = Field(min_length=1)
    validation_mse: Finite = Field(ge=0)
    code_commit: Text
    code_dirty: bool
    code_hash: Text
    environment: EnvironmentFingerprint
    parent_model_id: str | None = None
    version: int = Field(default=1, ge=1)
    generation_version: str = "learner-v1"

    @model_validator(mode="after")
    def check_lineage(self) -> Self:
        if self.artifact_hash != content_id(self.artifact):
            raise ValueError("model artifact hash does not match recorded artifact")
        if self.family != self.artifact.family:
            raise ValueError("model family does not match artifact")
        if not self.training_start <= self.training_end < self.validation_start:
            raise ValueError("training must precede validation")
        if self.validation_end < self.validation_start:
            raise ValueError("validation period is reversed")
        if self.created_at <= self.validation_end:
            raise ValueError("model creation must follow observed training and validation labels")
        if set(self.training_observation_ids) & set(self.validation_observation_ids):
            raise ValueError("training and validation observations overlap")
        if len(set(self.training_observation_ids)) != len(self.training_observation_ids):
            raise ValueError("training observations must be unique")
        if len(set(self.validation_observation_ids)) != len(self.validation_observation_ids):
            raise ValueError("validation observations must be unique")
        if (self.parent_model_id is None) != (self.version == 1):
            raise ValueError("successor versions require a parent model")
        return self


class Prediction(LearnerModel):
    prediction_id: Text
    model_id: Text
    features: FeatureRow
    evaluation_class: EvaluationClass
    created_at: datetime
    score: Finite
    rank: int = Field(ge=1)
    percentile: Finite = Field(ge=0, le=1)
    cohort_id: Text

    @model_validator(mode="after")
    def check_time(self) -> Self:
        if self.created_at < self.features.as_of:
            raise ValueError("prediction creation cannot precede features")
        if (
            self.evaluation_class is not EvaluationClass.HISTORICAL_REPLAY
            and self.created_at >= self.features.target_at
        ):
            raise ValueError("shadow prediction must exist before its target")
        if (
            self.evaluation_class is not EvaluationClass.HISTORICAL_REPLAY
            and self.created_at > self.features.as_of + timedelta(minutes=5)
        ):
            raise ValueError("prospective issuance deadline is five minutes after feature cutoff")
        return self


class Outcome(LearnerModel):
    prediction_id: Text
    resolved_at: datetime
    raw_return: Finite
    benchmark_return: Finite
    excess_return: Finite
    error: Finite
    source_hash: Text
    target_close: Finite = Field(gt=0)
    benchmark_close: Finite = Field(gt=0)
    available_at: datetime

    @model_validator(mode="after")
    def check_resolution(self) -> Self:
        if self.excess_return != self.raw_return - self.benchmark_return:
            raise ValueError("excess return must subtract benchmark return")
        if self.resolved_at < self.available_at:
            raise ValueError("outcome cannot resolve before source availability")
        return self


class Evaluation(LearnerModel):
    evaluation_id: Text
    model_id: Text
    evaluation_class: EvaluationClass
    created_at: datetime
    metrics_json: Text
    prediction_ids: tuple[str, ...] = Field(min_length=1)
    expected_prediction_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_predictions(self) -> Self:
        if len(set(self.prediction_ids)) != len(self.prediction_ids):
            raise ValueError("evaluation predictions must be unique")
        if len(set(self.expected_prediction_ids)) != len(self.expected_prediction_ids):
            raise ValueError("expected predictions must be unique")
        if not set(self.prediction_ids) <= set(self.expected_prediction_ids):
            raise ValueError("resolved predictions must be part of the expected cohort")
        return self
