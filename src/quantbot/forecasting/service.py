"""Orchestration for shadow forecasts, deliberately independent of the trading daemon."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol

from quantbot.domain import Bar
from quantbot.forecasting.models import (
    ForecastRecord,
    ForecastStatus,
    KronosConfig,
)
from quantbot.forecasting.provider import ForecastProviderError, SignalProvider
from quantbot.forecasting.snapshots import build_forecast_request


class ForecastSink(Protocol):
    def get_forecast(self, forecast_id: str) -> ForecastRecord | None: ...

    def save_forecast(self, record: ForecastRecord) -> bool: ...


class ShadowForecastService:
    """Run and optionally persist a forecast without any trading collaborators."""

    def __init__(
        self,
        provider: SignalProvider,
        *,
        ledger: ForecastSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._provider = provider
        self._ledger = ledger
        self._clock = clock or (lambda: datetime.now(UTC))

    def run(
        self,
        *,
        bars: Sequence[Bar],
        symbol: str,
        as_of: datetime,
        provider: str,
        vintage_id: str,
        available_at: datetime,
        lookback: int = 40,
        horizon: int = 12,
        config: KronosConfig | None = None,
        adjustment_metadata: Mapping[str, object] | None = None,
        future_timestamps: Sequence[datetime] | None = None,
        attempt_number: int = 0,
        persist: bool = True,
    ) -> ForecastRecord:
        request = build_forecast_request(
            bars=bars,
            symbol=symbol,
            as_of=as_of,
            provider=provider,
            vintage_id=vintage_id,
            available_at=available_at,
            lookback=lookback,
            horizon=horizon,
            config=config,
            adjustment_metadata=adjustment_metadata,
            future_timestamps=future_timestamps,
            attempt_number=attempt_number,
        )
        if persist and self._ledger is not None:
            existing = self._ledger.get_forecast(request.request_id)
            if existing is not None:
                return existing

        if len(request.snapshot.bars) < lookback:
            record = ForecastRecord(
                forecast_id=request.request_id,
                request=request,
                status=ForecastStatus.INSUFFICIENT_DATA,
                generated_at=self._clock(),
                error_code="INSUFFICIENT_LOOKBACK",
            )
        else:
            try:
                worker_forecast = self._provider.forecast(request)
            except ForecastProviderError as exc:
                record = ForecastRecord(
                    forecast_id=request.request_id,
                    request=request,
                    status=ForecastStatus.FAILED,
                    generated_at=self._clock(),
                    error_code=exc.code,
                )
            else:
                try:
                    record = ForecastRecord.succeeded(request, worker_forecast)
                except ValueError:
                    record = ForecastRecord(
                        forecast_id=request.request_id,
                        request=request,
                        status=ForecastStatus.FAILED,
                        generated_at=self._clock(),
                        error_code="WORKER_CONTRACT_MISMATCH",
                    )

        if persist and self._ledger is not None:
            self._ledger.save_forecast(record)
        return record
