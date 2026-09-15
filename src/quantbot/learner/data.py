"""Point-in-time daily feature rows and fixed-session supervised targets (#242).

The caller supplies availability timestamps.  They are enforced here but are evidence only when
the caller derived them from an authoritative cache/provider path.  A local JSON replay therefore
remains an explicitly exploratory input even when its timestamps are internally consistent.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from quantbot.domain import Bar
from quantbot.research.registry import DataRole, DataWindow
from quantbot.strategy import XNYSSessionSequence

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
DEFAULT_FEATURE_SET = "daily-alpha-v1"
DEFAULT_HORIZON = 5
MAX_LOOKBACK = 60

FEATURE_NAMES = (
    "return_1d",
    "return_5d",
    "return_20d",
    "return_60d",
    "realized_volatility_20d",
    "volume_change_20d",
    "relative_strength_20d",
    "distance_sma_20d",
    "distance_sma_60d",
    "momentum_acceleration",
    "market_return_20d",
    "market_volatility_20d",
)


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def normalize_datetime(cls, value: object) -> object:
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)


class DailyInput(FrozenModel):
    """One validated daily bar and the moment its value became knowable."""

    bar: Bar
    available_at: datetime

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.available_at < self.bar.timestamp:
            raise ValueError("a daily bar cannot be available before the session it describes")
        return self


class FeatureRow(FrozenModel):
    """Features knowable at one close, separate from their unresolved future outcome."""

    as_of: datetime
    input_start_at: datetime
    target_at: datetime
    asof_session_index: int = Field(ge=0)
    target_session_index: int = Field(ge=1)
    calendar_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    symbol: Text
    universe_id: Text
    source_id: Text
    feature_set: Text = DEFAULT_FEATURE_SET
    horizon: int = Field(default=DEFAULT_HORIZON, ge=1)
    benchmark_symbol: Text = "SPY"
    base_close: float = Field(gt=0, allow_inf_nan=False)
    benchmark_close: float = Field(gt=0, allow_inf_nan=False)
    values: tuple[float, ...]
    source_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @field_validator("symbol", "benchmark_symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        if value != value.upper():
            raise ValueError("symbols must be uppercase")
        return value

    @model_validator(mode="after")
    def validate_row(self) -> Self:
        if self.input_start_at > self.as_of:
            raise ValueError("input_start_at must be on or before as_of")
        if self.target_at <= self.as_of:
            raise ValueError("target_at must be after as_of")
        _validate_schema(self.feature_set, self.horizon)
        if self.target_session_index - self.asof_session_index != self.horizon:
            raise ValueError("target session index must be exactly horizon sessions after as_of")
        if len(self.values) != len(FEATURE_NAMES):
            raise ValueError(f"feature values must match the {len(FEATURE_NAMES)} fixed features")
        if not all(math.isfinite(value) for value in self.values):
            raise ValueError("feature values must be finite")
        return self

    @property
    def identity(self) -> str:
        return _canonical_hash(self.model_dump(mode="json"))


class LabelRow(FrozenModel):
    """A feature row after its fixed target session resolves."""

    features: FeatureRow
    role: DataRole
    raw_return: float = Field(allow_inf_nan=False)
    benchmark_return: float = Field(allow_inf_nan=False)
    excess_return: float = Field(allow_inf_nan=False)
    target_source_hash: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def validate_excess_return(self) -> Self:
        if self.excess_return != self.raw_return - self.benchmark_return:
            raise ValueError("excess_return must exactly equal raw_return - benchmark_return")
        return self

    @property
    def fit_eligible(self) -> bool:
        return self.role is DataRole.DISCOVERY

    @property
    def identity(self) -> str:
        return _canonical_hash(
            {
                "benchmark_return": self.benchmark_return,
                "excess_return": self.excess_return,
                "feature_identity": self.features.identity,
                "raw_return": self.raw_return,
                "role": self.role.value,
                "target_source_hash": self.target_source_hash,
            }
        )


class Dataset(FrozenModel):
    """Materialized train/validation rows plus a manifest-only protected window."""

    rows: tuple[LabelRow, ...] = Field(min_length=1)
    train_window: DataWindow
    validation_window: DataWindow
    holdout_window: DataWindow
    feature_names: tuple[str, ...] = FEATURE_NAMES
    universe_id: Text
    source_id: Text
    symbols: tuple[str, ...] = Field(min_length=1)

    @field_validator("rows")
    @classmethod
    def canonicalize_rows(cls, value: tuple[LabelRow, ...]) -> tuple[LabelRow, ...]:
        role_order = {DataRole.DISCOVERY: 0, DataRole.VALIDATION: 1}
        return tuple(
            sorted(
                value,
                key=lambda row: (
                    role_order.get(row.role, 2),
                    row.features.as_of,
                    row.features.symbol,
                    row.features.feature_set,
                    row.features.horizon,
                ),
            )
        )

    @model_validator(mode="after")
    def validate_dataset(self) -> Self:
        _validate_windows(self.train_window, self.validation_window, self.holdout_window)
        if self.feature_names != FEATURE_NAMES:
            raise ValueError("feature_names must use the fixed FEATURE_NAMES order")
        if self.symbols != tuple(sorted(set(self.symbols))):
            raise ValueError("dataset symbols must be unique and canonically sorted")
        seen: set[tuple[DataRole, str, datetime, str, int]] = set()
        for row in self.rows:
            if row.role not in {DataRole.DISCOVERY, DataRole.VALIDATION}:
                raise ValueError("protected and forward rows may not be materialized in a dataset")
            if row.features.universe_id != self.universe_id:
                raise ValueError("every feature row must use the dataset universe_id")
            if row.features.source_id != self.source_id:
                raise ValueError("every feature row must use the dataset source_id")
            observation_key = (
                row.role,
                row.features.symbol,
                row.features.as_of,
                row.features.feature_set,
                row.features.horizon,
            )
            if observation_key in seen:
                raise ValueError("duplicate learner observation for one role/symbol/session/schema")
            seen.add(observation_key)
            active = self.train_window if row.role is DataRole.DISCOVERY else self.validation_window
            next_window = (
                self.validation_window if row.role is DataRole.DISCOVERY else self.holdout_window
            )
            if not (active.start <= row.features.as_of.date() <= active.end):
                raise ValueError("a labeled row must start inside its declared role window")
            if row.features.target_at.date() > active.end:
                raise ValueError("a labeled row target must end inside its declared role window")
            if row.features.target_at.date() >= next_window.start:
                raise ValueError("a labeled row target must be purged before the next split")
        if {row.role for row in self.rows} != {DataRole.DISCOVERY, DataRole.VALIDATION}:
            raise ValueError("a dataset must contain both DISCOVERY and VALIDATION observations")
        if {row.features.symbol for row in self.rows} != set(self.symbols):
            raise ValueError("dataset symbols must match the materialized observation roster")
        return self

    @property
    def identity(self) -> str:
        return _canonical_hash(
            {
                "feature_names": self.feature_names,
                "holdout_window": self.holdout_window.model_dump(mode="json"),
                "rows": [row.identity for row in self.rows],
                "source_id": self.source_id,
                "symbols": self.symbols,
                "train_window": self.train_window.model_dump(mode="json"),
                "universe_id": self.universe_id,
                "validation_window": self.validation_window.model_dump(mode="json"),
            }
        )

    def rows_for_fit(self, *roles: DataRole) -> tuple[LabelRow, ...]:
        requested = frozenset(roles or (DataRole.DISCOVERY,))
        if requested != {DataRole.DISCOVERY}:
            raise ValueError("only DISCOVERY observations may be used for fitting")
        return tuple(row for row in self.rows if row.role in requested)


def _validate_windows(
    train_window: DataWindow,
    validation_window: DataWindow,
    holdout_window: DataWindow,
) -> None:
    if train_window.role is not DataRole.DISCOVERY:
        raise ValueError("train_window must be DISCOVERY")
    if validation_window.role is not DataRole.VALIDATION:
        raise ValueError("validation_window must be VALIDATION")
    if holdout_window.role is not DataRole.PROTECTED_EVALUATION:
        raise ValueError("holdout_window must be PROTECTED_EVALUATION")
    if len({train_window.dataset, validation_window.dataset, holdout_window.dataset}) != 1:
        raise ValueError("all split windows must name the same observation dataset")
    if len({train_window.snapshot, validation_window.snapshot, holdout_window.snapshot}) != 1:
        raise ValueError("all split windows must name the same snapshot")
    if train_window.end >= validation_window.start:
        raise ValueError("train_window must end before validation_window starts")
    if validation_window.end >= holdout_window.start:
        raise ValueError("validation_window must end before holdout_window starts")


def _validate_schema(feature_set: str, horizon: int) -> None:
    if feature_set != DEFAULT_FEATURE_SET or horizon != DEFAULT_HORIZON:
        raise ValueError(
            f"{DEFAULT_FEATURE_SET} is the fixed five-session feature schema; changing its "
            "name or horizon requires a new versioned implementation"
        )


def _symbols(symbols: Sequence[str], benchmark_symbol: str) -> tuple[str, ...]:
    normalized = tuple(symbols)
    if not normalized or len(set(normalized)) != len(normalized):
        raise ValueError("symbols must be nonempty and unique")
    if any(not value or value != value.strip() or value != value.upper() for value in normalized):
        raise ValueError("symbols must be uppercase without surrounding whitespace")
    if not benchmark_symbol or benchmark_symbol != benchmark_symbol.strip():
        raise ValueError("benchmark_symbol must not be blank or padded")
    if benchmark_symbol != benchmark_symbol.upper():
        raise ValueError("benchmark_symbol must be uppercase")
    if benchmark_symbol in normalized:
        raise ValueError("benchmark_symbol must not also be a learner symbol")
    return tuple(sorted(normalized))


def _input_index(
    inputs: Iterable[DailyInput], sessions: XNYSSessionSequence
) -> dict[tuple[str, datetime], DailyInput]:
    closes = {session.close_at for session in sessions.sessions}
    indexed: dict[tuple[str, datetime], DailyInput] = {}
    for item in inputs:
        if item.bar.timestamp not in closes:
            raise ValueError(
                f"daily input {item.bar.symbol}@{item.bar.timestamp.isoformat()} is not an exact "
                "session close"
            )
        key = (item.bar.symbol, item.bar.timestamp)
        if key in indexed:
            raise ValueError(
                f"duplicate daily input for {item.bar.symbol}@{item.bar.timestamp.isoformat()}"
            )
        indexed[key] = item
    return indexed


def _returns(closes: Sequence[float]) -> tuple[float, ...]:
    return tuple(
        current / previous - 1.0
        for previous, current in zip(closes, closes[1:], strict=False)
    )


def _feature_values(
    asset: Sequence[DailyInput], benchmark: Sequence[DailyInput]
) -> tuple[float, ...]:
    asset_closes = [float(item.bar.close) for item in asset]
    market_closes = [float(item.bar.close) for item in benchmark]
    asset_return_5 = asset_closes[-1] / asset_closes[-6] - 1.0
    asset_return_20 = asset_closes[-1] / asset_closes[-21] - 1.0
    market_return_20 = market_closes[-1] / market_closes[-21] - 1.0
    asset_daily = _returns(asset_closes[-21:])
    market_daily = _returns(market_closes[-21:])
    return (
        asset_closes[-1] / asset_closes[-2] - 1.0,
        asset_return_5,
        asset_return_20,
        asset_closes[-1] / asset_closes[-61] - 1.0,
        statistics.pstdev(asset_daily) * math.sqrt(252.0),
        float(asset[-1].bar.volume / asset[-21].bar.volume - 1),
        asset_return_20 - market_return_20,
        asset_closes[-1] / statistics.fmean(asset_closes[-20:]) - 1.0,
        asset_closes[-1] / statistics.fmean(asset_closes[-60:]) - 1.0,
        asset_return_5 - asset_return_20 / 4.0,
        market_return_20,
        statistics.pstdev(market_daily) * math.sqrt(252.0),
    )


def _row_at(
    indexed: dict[tuple[str, datetime], DailyInput],
    sessions: XNYSSessionSequence,
    *,
    session_index: int,
    symbol: str,
    universe_id: str,
    source_id: str,
    benchmark_symbol: str,
    feature_set: str,
    horizon: int,
) -> FeatureRow | None:
    if session_index < MAX_LOOKBACK or session_index + horizon >= len(sessions.sessions):
        return None
    as_of = sessions.sessions[session_index].close_at
    timestamps = tuple(
        session.close_at
        for session in sessions.sessions[session_index - MAX_LOOKBACK : session_index + 1]
    )
    asset = tuple(indexed.get((symbol, timestamp)) for timestamp in timestamps)
    benchmark = tuple(indexed.get((benchmark_symbol, timestamp)) for timestamp in timestamps)
    if any(item is None for item in (*asset, *benchmark)):
        return None
    known_asset = tuple(item for item in asset if item is not None)
    known_benchmark = tuple(item for item in benchmark if item is not None)
    if any(item.available_at > as_of for item in (*known_asset, *known_benchmark)):
        return None
    used = tuple(
        sorted(
            (*known_asset, *known_benchmark),
            key=lambda item: (item.bar.symbol, item.bar.timestamp),
        )
    )
    source_hash = _canonical_hash(
        [
            {
                "available_at": item.available_at.isoformat(),
                "bar": item.bar.model_dump(mode="json"),
            }
            for item in used
        ]
    )
    return FeatureRow(
        as_of=as_of,
        input_start_at=timestamps[0],
        target_at=sessions.sessions[session_index + horizon].close_at,
        asof_session_index=session_index,
        target_session_index=session_index + horizon,
        calendar_hash=_calendar_hash(sessions),
        symbol=symbol,
        universe_id=universe_id,
        source_id=source_id,
        feature_set=feature_set,
        horizon=horizon,
        benchmark_symbol=benchmark_symbol,
        base_close=float(known_asset[-1].bar.close),
        benchmark_close=float(known_benchmark[-1].bar.close),
        values=_feature_values(known_asset, known_benchmark),
        source_hash=source_hash,
    )


def _calendar_hash(sessions: XNYSSessionSequence) -> str:
    return _canonical_hash(
        {
            "calendar": sessions.calendar,
            "sessions": [session.model_dump(mode="json") for session in sessions.sessions],
        }
    )


def build_features(
    inputs: Sequence[DailyInput],
    sessions: XNYSSessionSequence,
    *,
    as_of: datetime,
    symbols: Sequence[str],
    universe_id: str,
    source_id: str,
    benchmark_symbol: str = "SPY",
    feature_set: str = DEFAULT_FEATURE_SET,
    horizon: int = DEFAULT_HORIZON,
) -> tuple[FeatureRow, ...]:
    """Build unresolved features at one exact close without reading future outcomes."""
    _validate_schema(feature_set, horizon)
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    normalized_as_of = as_of.astimezone(UTC)
    symbol_set = _symbols(symbols, benchmark_symbol)
    indexed = _input_index(inputs, sessions)
    try:
        session_index = next(
            index
            for index, session in enumerate(sessions.sessions)
            if session.close_at == normalized_as_of
        )
    except StopIteration as error:
        raise ValueError("as_of must be an exact session close") from error
    if session_index + horizon >= len(sessions.sessions):
        raise ValueError("session calendar does not cover the complete target horizon")
    return tuple(
        row
        for symbol in symbol_set
        if (
            row := _row_at(
                indexed,
                sessions,
                session_index=session_index,
                symbol=symbol,
                universe_id=universe_id,
                source_id=source_id,
                benchmark_symbol=benchmark_symbol,
                feature_set=feature_set,
                horizon=horizon,
            )
        )
        is not None
    )


def _window_indices(sessions: XNYSSessionSequence, window: DataWindow) -> range:
    dates = {session.session_date: index for index, session in enumerate(sessions.sessions)}
    if window.start not in dates or window.end not in dates:
        raise ValueError(f"{window.role.value} boundaries must be exact exchange sessions")
    return range(dates[window.start], dates[window.end] + 1)


def build_dataset(
    inputs: Sequence[DailyInput],
    sessions: XNYSSessionSequence,
    *,
    symbols: Sequence[str],
    universe_id: str,
    source_id: str,
    train_window: DataWindow,
    validation_window: DataWindow,
    holdout_window: DataWindow,
    benchmark_symbol: str = "SPY",
    feature_set: str = DEFAULT_FEATURE_SET,
    horizon: int = DEFAULT_HORIZON,
) -> Dataset:
    """Materialize chronological train/validation labels; keep the holdout manifest-only.

    Loaders must filter protected rows before constructing ``DailyInput`` objects.  This early
    check is a second line of defense: already-materialized rows beyond validation are refused
    before any feature or label calculation can inspect their values.
    """
    _validate_windows(train_window, validation_window, holdout_window)
    _validate_schema(feature_set, horizon)
    symbol_set = _symbols(symbols, benchmark_symbol)
    if any(item.bar.timestamp.date() > validation_window.end for item in inputs):
        raise ValueError(
            "daily inputs include protected or undeclared dates beyond the validation window"
        )
    indexed = _input_index(inputs, sessions)
    rows: list[LabelRow] = []
    for role, active, following in (
        (DataRole.DISCOVERY, train_window, validation_window),
        (DataRole.VALIDATION, validation_window, holdout_window),
    ):
        for session_index in _window_indices(sessions, active):
            target_index = session_index + horizon
            if target_index >= len(sessions.sessions):
                continue
            target_at = sessions.sessions[target_index].close_at
            if target_at.date() > active.end or target_at.date() >= following.start:
                continue
            for symbol in symbol_set:
                features = _row_at(
                    indexed,
                    sessions,
                    session_index=session_index,
                    symbol=symbol,
                    universe_id=universe_id,
                    source_id=source_id,
                    benchmark_symbol=benchmark_symbol,
                    feature_set=feature_set,
                    horizon=horizon,
                )
                if features is None:
                    raise ValueError(
                        f"missing exact feature history or unavailable feature input for "
                        f"{symbol}@{sessions.sessions[session_index].session_date}"
                    )
                asset_target = indexed.get((symbol, target_at))
                benchmark_target = indexed.get((benchmark_symbol, target_at))
                if asset_target is None or benchmark_target is None:
                    raise ValueError(
                        f"missing exact target for {symbol}@{target_at.date()} or "
                        f"{benchmark_symbol}@{target_at.date()}"
                    )
                if (
                    asset_target.available_at > target_at
                    or benchmark_target.available_at > target_at
                ):
                    raise ValueError(
                        f"target was unavailable at its declared session for "
                        f"{symbol}@{target_at.date()}"
                    )
                raw_return = float(asset_target.bar.close) / features.base_close - 1.0
                benchmark_return = (
                    float(benchmark_target.bar.close) / features.benchmark_close - 1.0
                )
                rows.append(
                    LabelRow(
                        features=features,
                        role=role,
                        raw_return=raw_return,
                        benchmark_return=benchmark_return,
                        excess_return=raw_return - benchmark_return,
                        target_source_hash=_canonical_hash(
                            [
                                {
                                    "available_at": item.available_at.isoformat(),
                                    "bar": item.bar.model_dump(mode="json"),
                                }
                                for item in (asset_target, benchmark_target)
                            ]
                        ),
                    )
                )
    return Dataset(
        rows=tuple(rows),
        train_window=train_window,
        validation_window=validation_window,
        holdout_window=holdout_window,
        universe_id=universe_id,
        source_id=source_id,
        symbols=symbol_set,
    )


__all__ = [
    "DEFAULT_FEATURE_SET",
    "DEFAULT_HORIZON",
    "FEATURE_NAMES",
    "DailyInput",
    "Dataset",
    "FeatureRow",
    "LabelRow",
    "build_dataset",
    "build_features",
]
