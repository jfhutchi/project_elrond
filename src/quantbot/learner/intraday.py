"""Pure minute-alpha-v1 research engine; no IO, clock, or trading authority.

Bar timestamps are interval starts. Prices are raw same-session closes. Volatility
is the population deviation of minute returns (unannualized). The orchestrator
owns real receipt/recording clocks, eligibility gates, trial charges and storage.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final, Literal, Self

from pydantic import Field, model_validator

from quantbot.domain import Bar
from quantbot.learner.estimators import FittedEstimator, fit_estimator, predict
from quantbot.learner.models import LearnerModel
from quantbot.research.manifest import content_id
from quantbot.strategy import XNYSSessionSequence

CONTRACT: Final = "minute-alpha-v1"
HORIZON_MINUTES = 30
FEATURE_NAMES = (
    "return_1m",
    "return_5m",
    "return_15m",
    "return_30m",
    "realized_volatility_15m",
    "volume_change_15m",
    "relative_strength_15m",
    "distance_sma_15m",
    "distance_sma_30m",
    "momentum_acceleration",
    "market_return_15m",
    "market_volatility_15m",
)
MINUTE = timedelta(minutes=1)
HORIZON = timedelta(minutes=HORIZON_MINUTES)
Text = Annotated[str, Field(min_length=1)]
Finite = Annotated[float, Field(allow_inf_nan=False)]
EvidenceClass = Literal["PROSPECTIVE_LOCAL", "EXPLORATORY_HISTORICAL"]


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


class IntradayFeatureRow(LearnerModel):
    contract: Literal["minute-alpha-v1"] = CONTRACT
    symbol: Text
    decision_at: datetime
    feature_at: datetime
    target_at: datetime
    values: tuple[Finite, ...]
    reference_close: Finite = Field(gt=0)
    benchmark_close: Finite = Field(gt=0)
    source_snapshot_hash: Text
    receipt_at: datetime
    evaluation_class: EvidenceClass = "PROSPECTIVE_LOCAL"

    @model_validator(mode="after")
    def check_contract(self) -> Self:
        if len(self.values) != len(FEATURE_NAMES):
            raise ValueError("values must match fixed intraday features")
        if self.target_at != self.feature_at + HORIZON:
            raise ValueError("target must be exactly thirty minutes after feature cutoff")
        if not self.feature_at <= self.decision_at <= self.feature_at + 2 * MINUTE:
            raise ValueError("feature cutoff must be completed and no more than two minutes old")
        if self.receipt_at < self.feature_at:
            raise ValueError("receipt cannot precede feature completion")
        if self.evaluation_class == "PROSPECTIVE_LOCAL" and self.receipt_at > self.decision_at:
            raise ValueError("receipt must precede prospective decision")
        return self


class IntradayLabel(LearnerModel):
    features: IntradayFeatureRow
    raw_return: Finite
    benchmark_return: Finite
    excess_return: Finite
    available_at: datetime
    source_snapshot_hash: Text
    evaluation_class: EvidenceClass

    @model_validator(mode="after")
    def check_outcome(self) -> Self:
        if self.available_at < self.features.target_at:
            raise ValueError("label availability cannot precede target")
        if self.excess_return != self.raw_return - self.benchmark_return:
            raise ValueError("excess return must subtract benchmark return")
        if self.evaluation_class != self.features.evaluation_class:
            raise ValueError("label evidence must match its features")
        return self

    @property
    def label_id(self) -> str:
        return content_id(self)


class IntradayModel(LearnerModel):
    contract: Literal["minute-alpha-v1"] = CONTRACT
    artifact: FittedEstimator
    feature_names: tuple[str, ...] = FEATURE_NAMES
    training_symbols: tuple[str, ...] = Field(min_length=1)
    created_at: datetime
    training_start: datetime
    training_end: datetime
    validation_start: datetime
    validation_end: datetime
    training_count: int = Field(ge=1)
    validation_count: int = Field(ge=1)
    training_label_ids: tuple[str, ...]
    validation_label_ids: tuple[str, ...]
    training_data_hash: Text
    validation_data_hash: Text
    validation_mse: Finite = Field(ge=0)
    code_commit: Text
    parent_model_id: Text | None = None

    @model_validator(mode="after")
    def check_contract(self) -> Self:
        if self.feature_names != FEATURE_NAMES or self.artifact.feature_names != FEATURE_NAMES:
            raise ValueError("model feature names must match intraday contract")
        if self.training_symbols != tuple(sorted(set(self.training_symbols))) or any(
            not symbol or symbol != symbol.strip().upper() or symbol == "SPY"
            for symbol in self.training_symbols
        ):
            raise ValueError(
                "model roster must contain sorted unique uppercase assets excluding SPY"
            )
        if self.artifact.family not in ("mean", "ridge"):
            raise ValueError("intraday estimator family must be mean or ridge")
        if self.artifact.alpha != 1.0 or self.artifact.seed != 0:
            raise ValueError("intraday fit requires alpha=1 and seed=0")
        if not self.training_start <= self.training_end < self.validation_start:
            raise ValueError("training targets must strictly precede validation")
        if not self.validation_start <= self.validation_end < self.created_at:
            raise ValueError("model creation must follow validation targets")
        ids = self.training_label_ids + self.validation_label_ids
        if len(set(ids)) != len(ids):
            raise ValueError("training and validation label IDs must be unique")
        if (len(self.training_label_ids), len(self.validation_label_ids)) != (
            self.training_count,
            self.validation_count,
        ):
            raise ValueError("label counts must match model lineage")
        return self

    @property
    def model_id(self) -> str:
        return content_id(self)

    @property
    def family(self) -> str:
        return self.artifact.family


class IntradayPrediction(LearnerModel):
    model_id: Text
    features: IntradayFeatureRow
    recorded_at: datetime
    score: Finite
    evidence_class: Literal["PROSPECTIVE_LOCAL"] = "PROSPECTIVE_LOCAL"

    @model_validator(mode="after")
    def check_clock(self) -> Self:
        if self.features.evaluation_class != "PROSPECTIVE_LOCAL":
            raise ValueError("historical features cannot issue prospective predictions")
        if (
            not self.features.decision_at
            <= self.recorded_at
            <= (self.features.decision_at + 5 * MINUTE)
            or self.recorded_at >= self.features.target_at
        ):
            raise ValueError("recorded_at must follow decision within five minutes before target")
        if self.recorded_at < self.features.receipt_at:
            raise ValueError("recording must follow receipt")
        return self

    @property
    def prediction_id(self) -> str:
        return content_id(self)


def _index(bars: Sequence[Bar]) -> dict[tuple[str, datetime], Bar]:
    indexed: dict[tuple[str, datetime], Bar] = {}
    for bar in bars:
        if bar.timestamp.second or bar.timestamp.microsecond:
            raise ValueError("minute bars must have aligned interval-start timestamps")
        key = (bar.symbol, bar.timestamp)
        if key in indexed:
            raise ValueError("duplicate symbol/minute bar")
        indexed[key] = bar
    return indexed


def _values(asset: Sequence[Bar], benchmark: Sequence[Bar]) -> tuple[float, ...]:
    closes = tuple(float(bar.close) for bar in asset)
    market = tuple(float(bar.close) for bar in benchmark)
    r5, r15, mr15 = (
        closes[-1] / closes[-6] - 1,
        closes[-1] / closes[-16] - 1,
        (market[-1] / market[-16] - 1),
    )

    def volatility(prices: tuple[float, ...]) -> float:
        return statistics.pstdev(
            b / a - 1 for a, b in zip(prices[-16:-1], prices[-15:], strict=True)
        )

    return (
        closes[-1] / closes[-2] - 1,
        r5,
        r15,
        closes[-1] / closes[-31] - 1,
        volatility(closes),
        float(asset[-1].volume / asset[-16].volume - 1),
        r15 - mr15,
        closes[-1] / statistics.fmean(closes[-15:]) - 1,
        closes[-1] / statistics.fmean(closes[-30:]) - 1,
        r5 - r15 / 3,
        mr15,
        volatility(market),
    )


def _build_features(
    indexed: dict[tuple[str, datetime], Bar],
    calendar: XNYSSessionSequence,
    *,
    decision_at: datetime,
    symbols: tuple[str, ...],
    source_snapshot_hash: str,
    receipt_at: datetime,
    evaluation_class: EvidenceClass,
) -> tuple[IntradayFeatureRow, ...]:
    decision_at, receipt_at = _aware(decision_at), _aware(receipt_at)
    if evaluation_class == "PROSPECTIVE_LOCAL" and receipt_at > decision_at:
        raise ValueError("receipt cannot follow decision")
    if (
        not symbols
        or len(set(symbols)) != len(symbols)
        or any(
            not symbol or symbol != symbol.strip().upper() or symbol == "SPY" for symbol in symbols
        )
    ):
        raise ValueError("symbols must be unique uppercase assets excluding SPY")
    session = next((s for s in calendar.sessions if s.open_at <= decision_at < s.close_at), None)
    if session is None:
        return ()
    required_symbols = (*sorted(symbols), "SPY")
    # Use the newest completed required-panel minute; never hide a missing newest
    # asset by sliding the whole cohort backwards to an older common timestamp.
    eligible = [
        timestamp
        for symbol, timestamp in indexed
        if symbol in required_symbols
        and session.open_at <= timestamp
        and timestamp + MINUTE <= decision_at
    ]
    if not eligible:
        return ()
    last = max(eligible)
    feature_at = last + MINUTE
    if decision_at - feature_at > 2 * MINUTE or feature_at + HORIZON > session.close_at:
        return ()
    first = last - 30 * MINUTE
    if first < session.open_at:
        return ()
    timestamps = tuple(first + i * MINUTE for i in range(31))
    if any(
        (symbol, timestamp) not in indexed
        for symbol in required_symbols
        for timestamp in timestamps
    ):
        return ()
    benchmark = tuple(indexed["SPY", timestamp] for timestamp in timestamps)
    rows = []
    for symbol in sorted(symbols):
        asset = tuple(indexed[symbol, timestamp] for timestamp in timestamps)
        rows.append(
            IntradayFeatureRow(
                symbol=symbol,
                decision_at=decision_at,
                feature_at=feature_at,
                target_at=feature_at + HORIZON,
                values=_values(asset, benchmark),
                reference_close=float(asset[-1].close),
                benchmark_close=float(benchmark[-1].close),
                source_snapshot_hash=source_snapshot_hash,
                receipt_at=receipt_at,
                evaluation_class=evaluation_class,
            )
        )
    return tuple(rows)


def build_features(
    bars: Sequence[Bar],
    calendar: XNYSSessionSequence,
    *,
    decision_at: datetime,
    symbols: tuple[str, ...],
    source_snapshot_hash: str,
    receipt_at: datetime,
) -> tuple[IntradayFeatureRow, ...]:
    """Build an entire complete cohort or none; malformed/duplicate data raises."""
    return _build_features(
        _index(bars),
        calendar,
        decision_at=decision_at,
        symbols=symbols,
        source_snapshot_hash=source_snapshot_hash,
        receipt_at=receipt_at,
        evaluation_class="PROSPECTIVE_LOCAL",
    )


def resolve_label(
    features: IntradayFeatureRow,
    bars: Sequence[Bar],
    *,
    resolved_at: datetime,
    receipt_at: datetime,
    source_snapshot_hash: str,
) -> IntradayLabel | None:
    """Resolve only elapsed exact target closes, preserving real source availability."""
    resolved_at, receipt_at = _aware(resolved_at), _aware(receipt_at)
    if not features.target_at <= receipt_at <= resolved_at:
        return None
    return _resolve_indexed(
        features,
        _index(bars),
        receipt_at=receipt_at,
        source_snapshot_hash=source_snapshot_hash,
    )


def _resolve_indexed(
    features: IntradayFeatureRow,
    indexed: dict[tuple[str, datetime], Bar],
    *,
    receipt_at: datetime,
    source_snapshot_hash: str,
) -> IntradayLabel | None:
    """Use a previously validated index only after the target/receipt clock check."""
    target_start = features.target_at - MINUTE
    asset = indexed.get((features.symbol, target_start))
    market = indexed.get(("SPY", target_start))
    if asset is None or market is None:
        return None
    raw = float(asset.close) / features.reference_close - 1
    benchmark = float(market.close) / features.benchmark_close - 1
    return IntradayLabel(
        features=features,
        raw_return=raw,
        benchmark_return=benchmark,
        excess_return=raw - benchmark,
        available_at=receipt_at,
        source_snapshot_hash=source_snapshot_hash,
        evaluation_class=features.evaluation_class,
    )


def build_historical_labels(
    bars: Sequence[Bar],
    calendar: XNYSSessionSequence,
    *,
    decision_times: Sequence[datetime],
    symbols: tuple[str, ...],
    source_snapshot_hash: str,
    observed_at: datetime,
) -> tuple[IntradayLabel, ...]:
    """Exploratory reconstruction, never evidence that an old forecast existed.

    Historical feature rows retain today's real ``observed_at`` receipt. They
    cannot be used by ``predict_intraday`` to issue prospective predictions.
    """
    observed_at = _aware(observed_at)
    times = tuple(_aware(value) for value in decision_times)
    if len(set(times)) != len(times):
        raise ValueError("duplicate historical decision time")
    indexed = _index(bars)
    labels = []
    for decision_at in sorted(times):
        if decision_at > observed_at:
            raise ValueError("historical decisions must already be observed")
        rows = _build_features(
            indexed,
            calendar,
            decision_at=decision_at,
            symbols=symbols,
            source_snapshot_hash=source_snapshot_hash,
            receipt_at=observed_at,
            evaluation_class="EXPLORATORY_HISTORICAL",
        )
        for row in rows:
            if row.target_at > observed_at:
                continue
            label = _resolve_indexed(
                row,
                indexed,
                receipt_at=observed_at,
                source_snapshot_hash=source_snapshot_hash,
            )
            if label is not None:
                labels.append(label)
    return tuple(labels)


def fit_intraday_model(
    labels: Sequence[IntradayLabel],
    *,
    validation_start: datetime,
    created_at: datetime,
    code_commit: str,
    family: str = "ridge",
    parent_model_id: str | None = None,
) -> IntradayModel:
    """One fixed fit with caller-selected chronological boundary and purged overlap."""
    validation_start, created_at = _aware(validation_start), _aware(created_at)
    if family not in ("mean", "ridge"):
        raise ValueError("intraday family must be mean or ridge")
    keys = [(label.features.symbol, label.features.decision_at) for label in labels]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate training label observation")
    roster = tuple(sorted({symbol for symbol, _ in keys}))
    cohorts: dict[datetime, set[str]] = {}
    for symbol, decision_at in keys:
        cohorts.setdefault(decision_at, set()).add(symbol)
    if any(symbols != set(roster) for symbols in cohorts.values()):
        raise ValueError("training requires a complete stable roster in every cohort")
    if any(label.available_at >= created_at for label in labels):
        raise ValueError("all training and validation labels must be observed before creation")
    ordered = sorted(labels, key=lambda label: (label.features.decision_at, label.features.symbol))
    training = tuple(label for label in ordered if label.features.target_at < validation_start)
    validation = tuple(label for label in ordered if label.features.decision_at >= validation_start)
    if not training or not validation:
        raise ValueError("nonempty purged training and validation partitions required")
    artifact = fit_estimator(
        tuple(label.features.values for label in training),
        tuple(label.excess_return for label in training),
        FEATURE_NAMES,
        family=family,
        alpha=1.0,
        seed=0,
    )
    scores = predict(artifact, tuple(label.features.values for label in validation))
    mse = statistics.fmean(
        (label.excess_return - score) ** 2 for label, score in zip(validation, scores, strict=True)
    )
    return IntradayModel(
        artifact=artifact,
        training_symbols=roster,
        created_at=created_at,
        training_start=min(label.features.decision_at for label in training),
        training_end=max(label.features.target_at for label in training),
        validation_start=validation_start,
        validation_end=max(label.features.target_at for label in validation),
        training_count=len(training),
        validation_count=len(validation),
        training_label_ids=tuple(label.label_id for label in training),
        validation_label_ids=tuple(label.label_id for label in validation),
        training_data_hash=content_id(training),
        validation_data_hash=content_id(validation),
        validation_mse=mse,
        code_commit=code_commit,
        parent_model_id=parent_model_id,
    )


def predict_intraday(
    model: IntradayModel,
    features: Sequence[IntradayFeatureRow],
    *,
    recorded_at: datetime,
) -> tuple[IntradayPrediction, ...]:
    """Bind each prospective score to a model that existed at decision time."""
    recorded_at = _aware(recorded_at)
    if tuple(sorted({row.symbol for row in features})) != model.training_symbols:
        raise ValueError("forecast roster must exactly match model training roster")
    if any(model.created_at > row.decision_at for row in features):
        raise ValueError("model must exist before forecast decision")
    cohort = {
        (row.decision_at, row.feature_at, row.target_at, row.receipt_at, row.source_snapshot_hash)
        for row in features
    }
    if len(cohort) > 1:
        raise ValueError("predictions must share one decision and source cohort")
    keys = [(row.symbol, row.decision_at) for row in features]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate feature observation")
    scores = predict(model.artifact, tuple(row.values for row in features))
    return tuple(
        IntradayPrediction(
            model_id=model.model_id, features=row, recorded_at=recorded_at, score=score
        )
        for row, score in zip(features, scores, strict=True)
    )
