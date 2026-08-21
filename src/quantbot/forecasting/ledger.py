"""Transaction-scoped immutable storage for shadow forecasts and realized outcomes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from sqlalchemy import select
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from quantbot.domain import Bar
from quantbot.forecasting.evaluation import score_forecast
from quantbot.forecasting.models import (
    BaselineScore,
    ForecastEvaluation,
    ForecastFeatures,
    ForecastRecord,
    ForecastRequest,
    ForecastStatus,
    WorkerForecast,
)
from quantbot.forecasting.schema import model_forecast_evaluations, model_forecasts
from quantbot.storage import RecordNotFoundError, StateConflictError
from quantbot.storage.database import decode_decimal, decode_utc, encode_decimal, encode_utc


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, str):
        raise TypeError("stored JSON must be text")
    loaded: object = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError("stored JSON must be an object")
    return cast(dict[str, object], loaded)


def _json_array(value: object) -> list[object]:
    if not isinstance(value, str):
        raise TypeError("stored JSON must be text")
    loaded: object = json.loads(value)
    if not isinstance(loaded, list):
        raise ValueError("stored JSON must be an array")
    return cast(list[object], loaded)


def _row_matches(row: RowMapping, payload: Mapping[str, object]) -> bool:
    return all(row[key] == value for key, value in payload.items())


def _forecast_payload(record: ForecastRecord) -> dict[str, object]:
    request = record.request
    snapshot = request.snapshot
    result_json = None
    if record.worker_forecast is not None and record.features is not None:
        result_json = _canonical_json(
            {
                "features": record.features.model_dump(mode="json"),
                "worker_forecast": record.worker_forecast.model_dump(mode="json"),
            }
        )
    return {
        "forecast_id": record.forecast_id,
        "evidence_role": record.evidence_role,
        "forecast_provider": "kronos",
        "model_id": request.config.model_id,
        "model_revision": request.config.model_revision,
        "tokenizer_id": request.config.tokenizer_id,
        "tokenizer_revision": request.config.tokenizer_revision,
        "kronos_code_revision": request.config.kronos_code_revision,
        "integration_version": request.config.integration_version,
        "symbol": snapshot.symbol,
        "as_of": encode_utc(snapshot.as_of),
        "latest_source_bar_at": (
            encode_utc(snapshot.latest_source_bar_at)
            if snapshot.latest_source_bar_at is not None
            else None
        ),
        "source_data_hash": snapshot.source_data_hash,
        "source_provider": snapshot.provider,
        "source_vintage_id": snapshot.vintage_id,
        "source_available_at": encode_utc(snapshot.available_at),
        "source_adjustment_metadata_json": snapshot.adjustment_metadata_json,
        "source_bar_count": len(snapshot.bars),
        "lookback": request.lookback,
        "horizon": request.horizon,
        "attempt_number": request.attempt_number,
        "status": record.status.value,
        "generated_at": encode_utc(record.generated_at),
        "request_json": _canonical_json(request.model_dump(mode="json")),
        "result_json": result_json,
        "error_code": record.error_code,
    }


def _evaluation_payload(evaluation: ForecastEvaluation) -> dict[str, object]:
    return {
        "evaluation_id": evaluation.evaluation_id,
        "forecast_id": evaluation.forecast_id,
        "evidence_role": evaluation.evidence_role,
        "scoring_version": evaluation.scoring_version,
        "evaluated_at": encode_utc(evaluation.evaluated_at),
        "realized_through": encode_utc(evaluation.realized_through),
        "outcome_provider": evaluation.outcome_provider,
        "outcome_vintage_id": evaluation.outcome_vintage_id,
        "outcome_available_at": encode_utc(evaluation.outcome_available_at),
        "outcome_adjustment_metadata_json": (evaluation.outcome_adjustment_metadata_json),
        "outcome_source_hash": evaluation.outcome_source_hash,
        "outcome_json": evaluation.outcome_json,
        "predicted_terminal_return": encode_decimal(evaluation.predicted_terminal_return),
        "realized_terminal_return": encode_decimal(evaluation.realized_terminal_return),
        "directionally_correct": evaluation.directionally_correct,
        "absolute_error": encode_decimal(evaluation.absolute_error),
        "squared_error": encode_decimal(evaluation.squared_error),
        "baselines_json": _canonical_json(
            [baseline.model_dump(mode="json") for baseline in evaluation.baselines]
        ),
    }


def _require_canonical_evaluation(forecast: ForecastRecord, evaluation: ForecastEvaluation) -> None:
    snapshot = forecast.request.snapshot
    if (
        evaluation.outcome_provider != snapshot.provider
        or evaluation.outcome_adjustment_metadata_json != snapshot.adjustment_metadata_json
    ):
        raise StateConflictError("evaluation outcome lineage does not match the immutable forecast")
    outcome_payload = _json_object(evaluation.outcome_json)
    raw_bars = outcome_payload.get("bars")
    if not isinstance(raw_bars, list):
        raise StateConflictError("evaluation outcome does not contain canonical bars")
    try:
        outcome_bars = tuple(Bar.model_validate(value) for value in raw_bars)
        expected = score_forecast(
            forecast,
            outcome_bars=outcome_bars,
            outcome_provider=evaluation.outcome_provider,
            outcome_vintage_id=evaluation.outcome_vintage_id,
            outcome_available_at=evaluation.outcome_available_at,
            outcome_adjustment_metadata=_json_object(evaluation.outcome_adjustment_metadata_json),
            evaluated_at=evaluation.evaluated_at,
        )
    except (TypeError, ValueError) as exc:
        raise StateConflictError(
            "evaluation does not match the canonical score for its linked forecast"
        ) from exc
    if expected != evaluation:
        raise StateConflictError(
            "evaluation does not match the canonical score for its linked forecast"
        )


class ForecastLedger:
    """Persist only append-only SHADOW artifacts inside a caller-owned transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save_forecast(self, record: ForecastRecord) -> bool:
        record = ForecastRecord.model_validate(record.model_dump(mode="json"))
        payload = _forecast_payload(record)
        row = (
            self._session.execute(
                select(model_forecasts).where(model_forecasts.c.forecast_id == record.forecast_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            self._session.execute(model_forecasts.insert().values(**payload))
            return True
        if _row_matches(row, payload):
            return False
        raise StateConflictError(f"forecast {record.forecast_id} conflicts with stored data")

    def get_forecast(self, forecast_id: str) -> ForecastRecord | None:
        row = (
            self._session.execute(
                select(model_forecasts).where(model_forecasts.c.forecast_id == forecast_id)
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._forecast_from_row(row)

    def list_forecasts(
        self,
        *,
        status: ForecastStatus | None = None,
        symbol: str | None = None,
    ) -> list[ForecastRecord]:
        statement = select(model_forecasts)
        if status is not None:
            statement = statement.where(model_forecasts.c.status == status.value)
        if symbol is not None:
            statement = statement.where(model_forecasts.c.symbol == symbol)
        rows = (
            self._session.execute(
                statement.order_by(model_forecasts.c.as_of, model_forecasts.c.forecast_id)
            )
            .mappings()
            .all()
        )
        return [self._forecast_from_row(row) for row in rows]

    @staticmethod
    def _forecast_from_row(row: RowMapping) -> ForecastRecord:
        request = ForecastRequest.model_validate(_json_object(row["request_json"]))
        result_raw = row["result_json"]
        worker: WorkerForecast | None = None
        features: ForecastFeatures | None = None
        if result_raw is not None:
            result = _json_object(result_raw)
            worker = WorkerForecast.model_validate(result["worker_forecast"])
            features = ForecastFeatures.model_validate(result["features"])
        error_raw = row["error_code"]
        record = ForecastRecord(
            forecast_id=str(row["forecast_id"]),
            evidence_role="SHADOW",
            request=request,
            status=ForecastStatus(str(row["status"])),
            generated_at=decode_utc(str(row["generated_at"])),
            worker_forecast=worker,
            features=features,
            error_code=str(error_raw) if error_raw is not None else None,
        )
        if not _row_matches(row, _forecast_payload(record)):
            raise StateConflictError(
                f"stored forecast {record.forecast_id} has inconsistent promoted fields"
            )
        return record

    def save_evaluation(self, evaluation: ForecastEvaluation) -> bool:
        evaluation = ForecastEvaluation.model_validate(evaluation.model_dump(mode="json"))
        forecast = self.get_forecast(evaluation.forecast_id)
        if forecast is None:
            raise RecordNotFoundError(f"forecast not found: {evaluation.forecast_id}")
        if forecast.status is not ForecastStatus.SUCCESS or forecast.features is None:
            raise StateConflictError("only successful forecasts can be evaluated")
        if evaluation.predicted_terminal_return != forecast.features.terminal_return_median:
            raise StateConflictError(
                "evaluation prediction does not match immutable forecast features"
            )
        _require_canonical_evaluation(forecast, evaluation)
        payload = _evaluation_payload(evaluation)
        row = (
            self._session.execute(
                select(model_forecast_evaluations).where(
                    model_forecast_evaluations.c.evaluation_id == evaluation.evaluation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            self._session.execute(model_forecast_evaluations.insert().values(**payload))
            return True
        if _row_matches(row, payload):
            return False
        raise StateConflictError(
            f"forecast evaluation {evaluation.evaluation_id} conflicts with stored data"
        )

    def get_evaluation(self, evaluation_id: str) -> ForecastEvaluation | None:
        row = (
            self._session.execute(
                select(model_forecast_evaluations).where(
                    model_forecast_evaluations.c.evaluation_id == evaluation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        baselines = tuple(
            BaselineScore.model_validate(value) for value in _json_array(row["baselines_json"])
        )
        evaluation = ForecastEvaluation(
            evaluation_id=str(row["evaluation_id"]),
            forecast_id=str(row["forecast_id"]),
            evidence_role="SHADOW",
            scoring_version=str(row["scoring_version"]),
            evaluated_at=decode_utc(str(row["evaluated_at"])),
            realized_through=decode_utc(str(row["realized_through"])),
            outcome_provider=str(row["outcome_provider"]),
            outcome_vintage_id=str(row["outcome_vintage_id"]),
            outcome_available_at=decode_utc(str(row["outcome_available_at"])),
            outcome_adjustment_metadata_json=str(row["outcome_adjustment_metadata_json"]),
            outcome_source_hash=str(row["outcome_source_hash"]),
            outcome_json=str(row["outcome_json"]),
            predicted_terminal_return=decode_decimal(str(row["predicted_terminal_return"])),
            realized_terminal_return=decode_decimal(str(row["realized_terminal_return"])),
            directionally_correct=bool(row["directionally_correct"]),
            absolute_error=decode_decimal(str(row["absolute_error"])),
            squared_error=decode_decimal(str(row["squared_error"])),
            baselines=baselines,
        )
        if not _row_matches(row, _evaluation_payload(evaluation)):
            raise StateConflictError(
                f"stored evaluation {evaluation.evaluation_id} has inconsistent fields"
            )
        return evaluation
