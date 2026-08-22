"""Immutable provider-neutral contracts for shadow candle forecasts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from statistics import median
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from quantbot.domain import Bar
from quantbot.domain.models import FiniteDecimal, FrozenModel, NonEmptyString

KRONOS_INTEGRATION_VERSION = "kronos-shadow-v1"
KRONOS_CODE_REVISION = "67b630e67f6a18c9e9be918d9b4337c960db1e9a"
KRONOS_MODEL_REVISION = "901c26c1332695a2a8f243eb2f37243a37bea320"
KRONOS_TOKENIZER_REVISION = "0e0117387f39004a9016484a186a908917e22426"
KRONOS_MODEL_WEIGHT_SHA256 = "b082dfcbd8e8c142a725c8bbb99781802f38fec81210e13479effb32b3c3e020"
KRONOS_MODEL_CONFIG_SHA256 = "5e0f6a605d5f81b5c9b559fe5cf716a1acb041c744e6f41bd05b097b7a685396"
KRONOS_TOKENIZER_WEIGHT_SHA256 = "59d85f6af76a2c3b8240ea06cb21db4213b4eeca053f246b23e29cf832fc6bee"
KRONOS_TOKENIZER_CONFIG_SHA256 = "2366e7ccfec76cbc19cf3c4c1b9c5d901be336ca1e83f2d2292c9bff381b77a2"
SCORING_VERSION = "shadow-terminal-return-v1"
KRONOS_WORKER_ENVIRONMENT_ID = "kronos-worker-py311-requirements-v1"


def _identity_hash(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


class ForecastStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class ForecastDirection(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    FLAT = "FLAT"


class KronosConfig(FrozenModel):
    """One explicit, identity-bearing zero-shot Kronos-small configuration."""

    model_id: NonEmptyString = "NeoQuasar/Kronos-small"
    model_revision: NonEmptyString = KRONOS_MODEL_REVISION
    model_weight_sha256: NonEmptyString = KRONOS_MODEL_WEIGHT_SHA256
    model_config_sha256: NonEmptyString = KRONOS_MODEL_CONFIG_SHA256
    tokenizer_id: NonEmptyString = "NeoQuasar/Kronos-Tokenizer-base"
    tokenizer_revision: NonEmptyString = KRONOS_TOKENIZER_REVISION
    tokenizer_weight_sha256: NonEmptyString = KRONOS_TOKENIZER_WEIGHT_SHA256
    tokenizer_config_sha256: NonEmptyString = KRONOS_TOKENIZER_CONFIG_SHA256
    kronos_code_revision: NonEmptyString = KRONOS_CODE_REVISION
    integration_version: NonEmptyString = KRONOS_INTEGRATION_VERSION
    temperature: Decimal = Field(default=Decimal("0.6"), gt=0, allow_inf_nan=False)
    top_p: Decimal = Field(default=Decimal("0.9"), gt=0, le=1, allow_inf_nan=False)
    top_k: int = Field(default=0, ge=0)
    sample_count: int = Field(default=10, ge=1, le=100)
    seed: int = Field(default=0, ge=0)
    device: Literal["cpu"] = "cpu"
    max_context: int = Field(default=512, ge=2, le=512)
    input_transform_version: Literal["ohlcv-no-amount-v1"] = "ohlcv-no-amount-v1"
    worker_environment_id: Literal["kronos-worker-py311-requirements-v1"] = (
        "kronos-worker-py311-requirements-v1"
    )

    @model_validator(mode="after")
    def require_small_pinned_model(self) -> Self:
        pinned = {
            "integration_version": KRONOS_INTEGRATION_VERSION,
            "kronos_code_revision": KRONOS_CODE_REVISION,
            "model_config_sha256": KRONOS_MODEL_CONFIG_SHA256,
            "model_id": "NeoQuasar/Kronos-small",
            "model_revision": KRONOS_MODEL_REVISION,
            "model_weight_sha256": KRONOS_MODEL_WEIGHT_SHA256,
            "tokenizer_config_sha256": KRONOS_TOKENIZER_CONFIG_SHA256,
            "tokenizer_id": "NeoQuasar/Kronos-Tokenizer-base",
            "tokenizer_revision": KRONOS_TOKENIZER_REVISION,
            "tokenizer_weight_sha256": KRONOS_TOKENIZER_WEIGHT_SHA256,
        }
        for field, expected in pinned.items():
            if getattr(self, field) != expected:
                raise ValueError(f"{field} is pinned by the first shadow integration")
        return self


class ForecastSourceSnapshot(FrozenModel):
    symbol: NonEmptyString
    as_of: datetime
    provider: NonEmptyString
    vintage_id: NonEmptyString
    available_at: datetime
    adjustment_metadata_json: NonEmptyString
    bars: tuple[Bar, ...]
    latest_source_bar_at: datetime | None
    source_data_hash: NonEmptyString

    @field_validator("symbol")
    @classmethod
    def uppercase_symbol(cls, value: str) -> str:
        if value != value.upper():
            raise ValueError("symbol must be uppercase")
        return value

    @model_validator(mode="after")
    def validate_point_in_time_snapshot(self) -> Self:
        timestamps = [bar.timestamp for bar in self.bars]
        if any(bar.symbol != self.symbol for bar in self.bars):
            raise ValueError("snapshot bars must match symbol")
        if timestamps != sorted(timestamps):
            raise ValueError("snapshot bars must be ordered")
        if len(timestamps) != len(set(timestamps)):
            raise ValueError("snapshot bars must not contain duplicate timestamps")
        if any(timestamp > self.as_of for timestamp in timestamps):
            raise ValueError("snapshot contains information after as_of")
        if self.available_at > self.as_of:
            raise ValueError("source vintage was not available at as_of")
        try:
            metadata: object = json.loads(self.adjustment_metadata_json)
        except json.JSONDecodeError as exc:
            raise ValueError("adjustment metadata must be canonical JSON") from exc
        if not isinstance(metadata, dict):
            raise ValueError("adjustment metadata must be a JSON object")
        canonical_metadata = json.dumps(
            metadata, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
        if canonical_metadata != self.adjustment_metadata_json:
            raise ValueError("adjustment metadata must use canonical JSON encoding")
        expected_latest = timestamps[-1] if timestamps else None
        if self.latest_source_bar_at != expected_latest:
            raise ValueError("latest_source_bar_at must identify the final snapshot bar")
        if expected_latest is not None and self.available_at < expected_latest:
            raise ValueError("source vintage cannot be available before its latest observation")
        expected_hash = calculate_source_data_hash(
            symbol=self.symbol,
            as_of=self.as_of,
            provider=self.provider,
            vintage_id=self.vintage_id,
            available_at=self.available_at,
            adjustment_metadata_json=self.adjustment_metadata_json,
            bars=self.bars,
        )
        if self.source_data_hash != expected_hash:
            raise ValueError("source_data_hash does not match the immutable snapshot")
        return self


def calculate_source_data_hash(
    *,
    symbol: str,
    as_of: datetime,
    provider: str,
    vintage_id: str,
    available_at: datetime,
    adjustment_metadata_json: str,
    bars: tuple[Bar, ...],
) -> str:
    return _identity_hash(
        {
            "adjustment_metadata_json": adjustment_metadata_json,
            "as_of": as_of.isoformat(),
            "available_at": available_at.isoformat(),
            "bars": [bar.model_dump(mode="json") for bar in bars],
            "provider": provider,
            "symbol": symbol,
            "vintage_id": vintage_id,
        }
    )


class ForecastRequest(FrozenModel):
    request_id: NonEmptyString
    snapshot: ForecastSourceSnapshot
    lookback: int = Field(ge=1, le=512)
    horizon: int = Field(ge=1, le=128)
    future_timestamps: tuple[datetime, ...]
    config: KronosConfig
    attempt_number: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_horizon(self) -> Self:
        if self.lookback > self.config.max_context:
            raise ValueError("lookback cannot exceed the model context")
        if len(self.future_timestamps) != self.horizon:
            raise ValueError("future timestamp count must equal horizon")
        if list(self.future_timestamps) != sorted(self.future_timestamps):
            raise ValueError("future timestamps must be ordered")
        if len(set(self.future_timestamps)) != len(self.future_timestamps):
            raise ValueError("future timestamps must be unique")
        if any(timestamp <= self.snapshot.as_of for timestamp in self.future_timestamps):
            raise ValueError("future timestamps must be after as_of")
        expected_id = calculate_request_id(
            snapshot=self.snapshot,
            lookback=self.lookback,
            horizon=self.horizon,
            future_timestamps=self.future_timestamps,
            config=self.config,
            attempt_number=self.attempt_number,
        )
        if self.request_id != expected_id:
            raise ValueError("request_id does not match forecast request content")
        return self


def calculate_request_id(
    *,
    snapshot: ForecastSourceSnapshot,
    lookback: int,
    horizon: int,
    future_timestamps: tuple[datetime, ...],
    config: KronosConfig,
    attempt_number: int,
) -> str:
    return _identity_hash(
        {
            "as_of": snapshot.as_of.isoformat(),
            "attempt_number": attempt_number,
            "config": config.model_dump(mode="json"),
            "future_timestamps": [value.isoformat() for value in future_timestamps],
            "horizon": horizon,
            "lookback": lookback,
            "source_data_hash": snapshot.source_data_hash,
            "symbol": snapshot.symbol,
        }
    )


class ForecastCandle(FrozenModel):
    open: Decimal = Field(gt=0, allow_inf_nan=False)
    high: Decimal = Field(gt=0, allow_inf_nan=False)
    low: Decimal = Field(gt=0, allow_inf_nan=False)
    close: Decimal = Field(gt=0, allow_inf_nan=False)
    volume: Decimal = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_ohlc(self) -> Self:
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("forecast high violates OHLC ordering")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("forecast low violates OHLC ordering")
        return self


class WorkerForecast(FrozenModel):
    request_id: NonEmptyString
    sample_paths: tuple[tuple[ForecastCandle, ...], ...]
    model_revision: NonEmptyString
    tokenizer_revision: NonEmptyString
    kronos_code_revision: NonEmptyString
    generated_at: datetime
    runtime_environment_hash: NonEmptyString
    device: NonEmptyString
    model_initialization_seconds: Decimal = Field(ge=0, allow_inf_nan=False)
    inference_seconds: Decimal = Field(ge=0, allow_inf_nan=False)
    peak_process_memory_bytes: int | None = Field(default=None, ge=0)
    peak_gpu_memory_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_paths(self) -> Self:
        if not self.sample_paths or not self.sample_paths[0]:
            raise ValueError("worker must return at least one non-empty sample path")
        path_length = len(self.sample_paths[0])
        if any(len(path) != path_length for path in self.sample_paths):
            raise ValueError("all sample paths must have the same horizon")
        return self


class ForecastFeatures(FrozenModel):
    terminal_return_mean: FiniteDecimal
    terminal_return_median: FiniteDecimal
    high_excursion: FiniteDecimal
    low_excursion: FiniteDecimal
    direction: ForecastDirection
    sample_terminal_return_dispersion: FiniteDecimal


def derive_forecast_features(
    worker: WorkerForecast,
    *,
    actual_close: Decimal,
) -> ForecastFeatures:
    """Derive descriptive forecast features; dispersion is deliberately not confidence."""
    terminal_returns = [path[-1].close / actual_close - 1 for path in worker.sample_paths]
    mean_return = sum(terminal_returns, start=Decimal(0)) / len(terminal_returns)
    median_return = Decimal(median(terminal_returns))
    variance = sum(
        ((value - mean_return) ** 2 for value in terminal_returns), start=Decimal(0)
    ) / len(terminal_returns)
    high_excursion = max(candle.high for path in worker.sample_paths for candle in path)
    low_excursion = min(candle.low for path in worker.sample_paths for candle in path)
    direction = (
        ForecastDirection.UP
        if median_return > 0
        else ForecastDirection.DOWN
        if median_return < 0
        else ForecastDirection.FLAT
    )
    return ForecastFeatures(
        terminal_return_mean=mean_return,
        terminal_return_median=median_return,
        high_excursion=high_excursion / actual_close - 1,
        low_excursion=low_excursion / actual_close - 1,
        direction=direction,
        sample_terminal_return_dispersion=variance.sqrt(),
    )


class ForecastRecord(FrozenModel):
    forecast_id: NonEmptyString
    evidence_role: Literal["SHADOW"] = "SHADOW"
    request: ForecastRequest
    status: ForecastStatus
    generated_at: datetime
    worker_forecast: WorkerForecast | None = None
    features: ForecastFeatures | None = None
    error_code: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_status_payload(self) -> Self:
        if self.forecast_id != self.request.request_id:
            raise ValueError("forecast identity must match request identity")
        if self.generated_at < self.request.snapshot.as_of:
            raise ValueError("forecast cannot be generated before its point-in-time cutoff")
        succeeded = self.status is ForecastStatus.SUCCESS
        if succeeded != (self.worker_forecast is not None and self.features is not None):
            raise ValueError("successful forecasts require worker output and features")
        if succeeded == (self.error_code is not None):
            raise ValueError("only unsuccessful forecasts require an error code")
        if succeeded:
            assert self.worker_forecast is not None
            assert self.features is not None
            worker = self.worker_forecast
            if self.generated_at != worker.generated_at:
                raise ValueError("forecast timestamp must match immutable worker output")
            if worker.request_id != self.request.request_id:
                raise ValueError("worker response does not match request")
            if len(worker.sample_paths) != self.request.config.sample_count:
                raise ValueError("worker sample count does not match request")
            if any(len(path) != self.request.horizon for path in worker.sample_paths):
                raise ValueError("worker horizon does not match request")
            if worker.model_revision != self.request.config.model_revision:
                raise ValueError("worker model revision does not match request")
            if worker.tokenizer_revision != self.request.config.tokenizer_revision:
                raise ValueError("worker tokenizer revision does not match request")
            if worker.kronos_code_revision != self.request.config.kronos_code_revision:
                raise ValueError("worker Kronos revision does not match request")
            if worker.device != self.request.config.device:
                raise ValueError("worker device does not match request")
            if not self.request.snapshot.bars:
                raise ValueError("a successful forecast requires source bars")
            expected_features = derive_forecast_features(
                worker, actual_close=self.request.snapshot.bars[-1].close
            )
            if self.features != expected_features:
                raise ValueError("forecast features do not match immutable worker output")
        return self

    @classmethod
    def succeeded(cls, request: ForecastRequest, worker: WorkerForecast) -> ForecastRecord:
        if worker.request_id != request.request_id:
            raise ValueError("worker response does not match request")
        if len(worker.sample_paths) != request.config.sample_count:
            raise ValueError("worker sample count does not match request")
        if any(len(path) != request.horizon for path in worker.sample_paths):
            raise ValueError("worker horizon does not match request")
        if worker.model_revision != request.config.model_revision:
            raise ValueError("worker model revision does not match request")
        if worker.tokenizer_revision != request.config.tokenizer_revision:
            raise ValueError("worker tokenizer revision does not match request")
        if worker.kronos_code_revision != request.config.kronos_code_revision:
            raise ValueError("worker Kronos revision does not match request")
        if worker.device != request.config.device:
            raise ValueError("worker device does not match request")
        if not request.snapshot.bars:
            raise ValueError("a successful forecast requires source bars")
        return cls(
            forecast_id=request.request_id,
            request=request,
            status=ForecastStatus.SUCCESS,
            generated_at=worker.generated_at,
            worker_forecast=worker,
            features=derive_forecast_features(worker, actual_close=request.snapshot.bars[-1].close),
        )


def _same_direction(predicted: Decimal, realized: Decimal) -> bool:
    if predicted == 0 or realized == 0:
        return predicted == realized
    return (predicted > 0) == (realized > 0)


class BaselineScore(FrozenModel):
    name: NonEmptyString
    predicted_terminal_return: FiniteDecimal
    directionally_correct: bool
    absolute_error: FiniteDecimal
    squared_error: FiniteDecimal

    @model_validator(mode="after")
    def validate_error_shape(self) -> Self:
        if self.absolute_error < 0 or self.squared_error < 0:
            raise ValueError("baseline error measures must be non-negative")
        if self.squared_error != self.absolute_error**2:
            raise ValueError("baseline squared error must equal absolute error squared")
        return self


def calculate_evaluation_id(
    *, forecast_id: str, outcome_source_hash: str, scoring_version: str
) -> str:
    return _identity_hash(
        {
            "forecast_id": forecast_id,
            "outcome_source_hash": outcome_source_hash,
            "scoring_version": scoring_version,
        }
    )


class ForecastEvaluation(FrozenModel):
    evaluation_id: NonEmptyString
    forecast_id: NonEmptyString
    evidence_role: Literal["SHADOW"] = "SHADOW"
    scoring_version: Literal["shadow-terminal-return-v1"] = "shadow-terminal-return-v1"
    evaluated_at: datetime
    realized_through: datetime
    outcome_provider: NonEmptyString
    outcome_vintage_id: NonEmptyString
    outcome_available_at: datetime
    outcome_adjustment_metadata_json: NonEmptyString
    outcome_source_hash: NonEmptyString
    outcome_json: NonEmptyString
    predicted_terminal_return: FiniteDecimal
    realized_terminal_return: FiniteDecimal
    directionally_correct: bool
    absolute_error: FiniteDecimal
    squared_error: FiniteDecimal
    baselines: tuple[BaselineScore, ...]

    @model_validator(mode="after")
    def validate_evaluation(self) -> Self:
        if self.outcome_available_at < self.realized_through:
            raise ValueError("outcome vintage cannot predate the realized observation")
        if self.evaluated_at != self.outcome_available_at:
            raise ValueError("evaluated_at must be the canonical outcome availability time")
        try:
            adjustment_metadata: object = json.loads(self.outcome_adjustment_metadata_json)
            outcome_payload: object = json.loads(self.outcome_json)
        except json.JSONDecodeError as exc:
            raise ValueError("outcome provenance must be valid canonical JSON") from exc
        if not isinstance(adjustment_metadata, dict) or not isinstance(outcome_payload, dict):
            raise ValueError("outcome provenance must contain JSON objects")
        canonical_adjustment = json.dumps(
            adjustment_metadata, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
        canonical_outcome = json.dumps(
            outcome_payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
        if canonical_adjustment != self.outcome_adjustment_metadata_json:
            raise ValueError("outcome adjustment metadata must use canonical JSON encoding")
        if canonical_outcome != self.outcome_json:
            raise ValueError("outcome_json must use canonical JSON encoding")
        promoted = {
            "adjustment_metadata_json": self.outcome_adjustment_metadata_json,
            "available_at": self.outcome_available_at.isoformat(),
            "provider": self.outcome_provider,
            "scoring_version": self.scoring_version,
            "vintage_id": self.outcome_vintage_id,
        }
        for field, expected in promoted.items():
            if outcome_payload.get(field) != expected:
                raise ValueError(f"outcome_json {field} does not match promoted provenance")
        if _identity_hash(outcome_payload) != self.outcome_source_hash:
            raise ValueError("outcome_source_hash does not match outcome_json")
        expected_id = calculate_evaluation_id(
            forecast_id=self.forecast_id,
            outcome_source_hash=self.outcome_source_hash,
            scoring_version=self.scoring_version,
        )
        if self.evaluation_id != expected_id:
            raise ValueError("evaluation_id does not match immutable outcome identity")
        error = self.predicted_terminal_return - self.realized_terminal_return
        if self.absolute_error != abs(error) or self.squared_error != error**2:
            raise ValueError("forecast error measures do not match returns")
        if self.directionally_correct != _same_direction(
            self.predicted_terminal_return, self.realized_terminal_return
        ):
            raise ValueError("forecast direction score does not match returns")
        names = tuple(score.name for score in self.baselines)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("baseline names must be sorted and unique")
        for score in self.baselines:
            baseline_error = score.predicted_terminal_return - self.realized_terminal_return
            if score.absolute_error != abs(baseline_error):
                raise ValueError("baseline absolute error does not match realized return")
            if score.directionally_correct != _same_direction(
                score.predicted_terminal_return, self.realized_terminal_return
            ):
                raise ValueError("baseline direction score does not match returns")
        return self
