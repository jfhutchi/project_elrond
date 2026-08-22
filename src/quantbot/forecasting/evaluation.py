"""Append-only realized-outcome scoring for mature shadow forecasts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from quantbot.domain import Bar
from quantbot.forecasting.models import (
    SCORING_VERSION,
    BaselineScore,
    ForecastEvaluation,
    ForecastRecord,
    ForecastStatus,
    calculate_evaluation_id,
)


class UnscoreableForecast(RuntimeError):
    """A forecast whose targets can never be realized, as distinct from not yet realized.

    Raised rather than returned so a caller cannot treat it as "keep waiting". Scoring never
    substitutes a nearby session for a missing one -- the forecast was registered against exact
    timestamps and scoring against different ones would be scoring a different forecast.
    """

    def __init__(
        self, message: str, *, forecast_id: str, missing_targets: tuple[datetime, ...]
    ) -> None:
        super().__init__(message)
        self.forecast_id = forecast_id
        self.missing_targets = missing_targets


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _same_direction(predicted: Decimal, realized: Decimal) -> bool:
    if predicted == 0 or realized == 0:
        return predicted == realized
    return (predicted > 0) == (realized > 0)


def _baseline_score(name: str, predicted: Decimal, realized: Decimal) -> BaselineScore:
    error = predicted - realized
    return BaselineScore(
        name=name,
        predicted_terminal_return=predicted,
        directionally_correct=_same_direction(predicted, realized),
        absolute_error=abs(error),
        squared_error=error**2,
    )


def _trailing_return(bars: Sequence[Bar], length: int) -> Decimal:
    selected = bars[-min(length, len(bars)) :]
    if len(selected) < 2:
        return Decimal(0)
    return selected[-1].close / selected[0].close - 1


def score_forecast(
    forecast: ForecastRecord,
    *,
    outcome_bars: Sequence[Bar],
    outcome_provider: str,
    outcome_vintage_id: str,
    outcome_available_at: datetime,
    outcome_adjustment_metadata: Mapping[str, object] | None,
    evaluated_at: datetime,
) -> ForecastEvaluation | None:
    """Score a successful forecast only after all horizon observations are knowable."""
    if forecast.status is not ForecastStatus.SUCCESS or forecast.features is None:
        return None
    snapshot = forecast.request.snapshot
    if snapshot.latest_source_bar_at is None or not snapshot.bars:
        return None
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("evaluated_at must be timezone-aware")
    if outcome_available_at.tzinfo is None or outcome_available_at.utcoffset() is None:
        raise ValueError("outcome_available_at must be timezone-aware")
    cutoff = evaluated_at.astimezone(UTC)
    available_at = outcome_available_at.astimezone(UTC)
    targets = forecast.request.future_timestamps
    if cutoff < targets[-1] or available_at > cutoff:
        return None
    if available_at < targets[-1]:
        raise ValueError("outcome vintage cannot be available before the final target")
    matching = [
        bar
        for bar in outcome_bars
        if bar.symbol == snapshot.symbol and bar.timestamp in set(targets)
    ]
    if len({bar.timestamp for bar in matching}) != len(matching):
        raise ValueError("duplicate outcome bar timestamps are not allowed")
    by_timestamp = {bar.timestamp: bar for bar in matching}
    if missing := [timestamp for timestamp in targets if timestamp not in by_timestamp]:
        # Reaching here means `available_at >= targets[-1]`: the outcome vintage already covers
        # the whole target window and the bar still is not in it. That is unreachable, not
        # pending, and returning None would make the two indistinguishable -- a forecast waiting
        # forever on a session that never happened, reported as success-shaped emptiness.
        #
        # Default targets skip weekends but deliberately do not guess exchange holidays, so this
        # fires several times a year. Not guessing is right; a forecast silently stuck because of
        # it is not.
        raise UnscoreableForecast(
            f"{forecast.forecast_id} registered {len(missing)} target(s) with no bar in an "
            f"outcome vintage that already covers them: "
            f"{', '.join(timestamp.isoformat() for timestamp in missing)}",
            forecast_id=forecast.forecast_id,
            missing_targets=tuple(missing),
        )
    selected = [by_timestamp[timestamp] for timestamp in targets]
    source_close = snapshot.bars[-1].close
    realized = selected[-1].close / source_close - 1
    predicted = forecast.features.terminal_return_median
    error = predicted - realized

    momentum = _trailing_return(snapshot.bars, 20)
    reversal = -_trailing_return(snapshot.bars, 5)
    baselines = (
        _baseline_score("momentum", momentum, realized),
        _baseline_score("persistence", Decimal(0), realized),
        _baseline_score("short_reversal", reversal, realized),
    )
    adjustment_metadata_json = _canonical_json(dict(outcome_adjustment_metadata or {}))
    outcome_payload = {
        "adjustment_metadata_json": adjustment_metadata_json,
        "available_at": available_at.isoformat(),
        "bars": [bar.model_dump(mode="json") for bar in selected],
        "provider": outcome_provider,
        "scoring_version": SCORING_VERSION,
        "vintage_id": outcome_vintage_id,
    }
    outcome_json = _canonical_json(outcome_payload)
    outcome_hash = hashlib.sha256(outcome_json.encode("utf-8")).hexdigest()
    evaluation_id = calculate_evaluation_id(
        forecast_id=forecast.forecast_id,
        outcome_source_hash=outcome_hash,
        scoring_version=SCORING_VERSION,
    )
    return ForecastEvaluation(
        evaluation_id=evaluation_id,
        forecast_id=forecast.forecast_id,
        scoring_version=SCORING_VERSION,
        evaluated_at=available_at,
        realized_through=selected[-1].timestamp,
        outcome_provider=outcome_provider,
        outcome_vintage_id=outcome_vintage_id,
        outcome_available_at=available_at,
        outcome_adjustment_metadata_json=adjustment_metadata_json,
        outcome_source_hash=outcome_hash,
        outcome_json=outcome_json,
        predicted_terminal_return=predicted,
        realized_terminal_return=realized,
        directionally_correct=_same_direction(predicted, realized),
        absolute_error=abs(error),
        squared_error=error**2,
        baselines=baselines,
    )
