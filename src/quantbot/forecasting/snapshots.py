"""Point-in-time snapshot construction and deterministic forecast identities."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from quantbot.domain import Bar
from quantbot.forecasting.models import (
    ForecastRequest,
    ForecastSourceSnapshot,
    KronosConfig,
    calculate_request_id,
    calculate_source_data_hash,
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _normalized_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    return value.astimezone(UTC)


def daily_future_timestamps(
    as_of: datetime,
    horizon: int,
    *,
    session_timestamp: datetime | None = None,
) -> tuple[datetime, ...]:
    """Return weekday timestamps for Kronos temporal features.

    Exchange holidays are intentionally not guessed. Callers with an authoritative calendar can
    provide explicit timestamps to ``build_forecast_request``.
    """
    if horizon <= 0:
        raise ValueError("horizon must be positive")
    cutoff = _normalized_utc(as_of)
    session_clock = _normalized_utc(session_timestamp or as_of)
    current_date = cutoff.date()
    values: list[datetime] = []
    while len(values) < horizon:
        candidate = datetime.combine(current_date, session_clock.timetz()).astimezone(UTC)
        if candidate > cutoff and candidate.weekday() < 5:
            values.append(candidate)
        current_date += timedelta(days=1)
    return tuple(values)


def build_forecast_request(
    *,
    bars: Sequence[Bar],
    symbol: str,
    as_of: datetime,
    provider: str,
    vintage_id: str,
    available_at: datetime,
    lookback: int,
    horizon: int,
    config: KronosConfig | None = None,
    adjustment_metadata: Mapping[str, object] | None = None,
    future_timestamps: Sequence[datetime] | None = None,
    attempt_number: int = 0,
) -> ForecastRequest:
    """Freeze only observations knowable at ``as_of`` into an identity-bearing request."""
    normalized_as_of = _normalized_utc(as_of)
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        raise ValueError("symbol must not be empty")
    if not provider.strip():
        raise ValueError("provider must not be empty")
    if not vintage_id.strip():
        raise ValueError("vintage_id must not be empty")
    if not 1 <= lookback <= 512:
        raise ValueError("lookback must be between 1 and 512")
    if not 1 <= horizon <= 128:
        raise ValueError("horizon must be between 1 and 128")

    eligible = [bar for bar in bars if bar.timestamp <= normalized_as_of]
    if any(bar.symbol != normalized_symbol for bar in eligible):
        raise ValueError("all eligible bars must match the requested symbol")
    eligible.sort(key=lambda bar: bar.timestamp)
    timestamps = [bar.timestamp for bar in eligible]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError("duplicate source bar timestamps are not allowed")
    selected = tuple(eligible[-lookback:])

    normalized_available_at = _normalized_utc(available_at)
    metadata_json = _canonical_json({} if adjustment_metadata is None else adjustment_metadata)
    source_data_hash = calculate_source_data_hash(
        symbol=normalized_symbol,
        as_of=normalized_as_of,
        provider=provider.strip(),
        vintage_id=vintage_id.strip(),
        available_at=normalized_available_at,
        adjustment_metadata_json=metadata_json,
        bars=selected,
    )
    snapshot = ForecastSourceSnapshot(
        symbol=normalized_symbol,
        as_of=normalized_as_of,
        provider=provider.strip(),
        vintage_id=vintage_id.strip(),
        available_at=normalized_available_at,
        adjustment_metadata_json=metadata_json,
        bars=selected,
        latest_source_bar_at=selected[-1].timestamp if selected else None,
        source_data_hash=source_data_hash,
    )
    resolved_config = config or KronosConfig()
    resolved_future = tuple(
        _normalized_utc(timestamp)
        for timestamp in (
            future_timestamps
            if future_timestamps is not None
            else daily_future_timestamps(
                normalized_as_of,
                horizon,
                session_timestamp=selected[-1].timestamp if selected else None,
            )
        )
    )
    request_id = calculate_request_id(
        snapshot=snapshot,
        lookback=lookback,
        horizon=horizon,
        future_timestamps=resolved_future,
        config=resolved_config,
        attempt_number=attempt_number,
    )
    return ForecastRequest(
        request_id=request_id,
        snapshot=snapshot,
        lookback=lookback,
        horizon=horizon,
        future_timestamps=resolved_future,
        config=resolved_config,
        attempt_number=attempt_number,
    )
