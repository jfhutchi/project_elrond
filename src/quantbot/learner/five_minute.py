"""Separate price-only 5Min research contract; no IO, fitting or order authority.

Published bars use interval-start timestamps. Missing bars reject the whole
cohort. This contract cannot consume minute-alpha-v1 models or feature rows.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal, Self

from pydantic import Field, model_validator

from quantbot.domain import Bar
from quantbot.learner.intraday import EvidenceClass, Finite, Text, _aware
from quantbot.learner.models import LearnerModel
from quantbot.strategy import XNYSSessionSequence

CONTRACT: Literal["five-minute-price-alpha-v1"] = "five-minute-price-alpha-v1"
INTERVAL = timedelta(minutes=5)
HORIZON = timedelta(minutes=30)
FEATURE_NAMES = (
    "return_5m",
    "return_15m",
    "return_30m",
    "realized_volatility_15m",
    "relative_strength_15m",
    "distance_sma_15m",
    "distance_sma_30m",
    "momentum_acceleration",
    "market_return_15m",
    "market_volatility_15m",
)


class FiveMinuteFeatureRow(LearnerModel):
    contract: Literal["five-minute-price-alpha-v1"] = CONTRACT
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
    def check(self) -> Self:
        for timestamp in (self.decision_at, self.feature_at, self.target_at, self.receipt_at):
            _aware(timestamp)
        if len(self.values) != len(FEATURE_NAMES):
            raise ValueError("values must match five-minute contract")
        if self.target_at != self.feature_at + HORIZON:
            raise ValueError("target must be exactly thirty minutes after cutoff")
        if not self.feature_at <= self.decision_at <= self.feature_at + timedelta(minutes=2):
            raise ValueError("feature cutoff must be completed and at most two minutes old")
        if self.receipt_at < self.feature_at or (
            self.evaluation_class == "PROSPECTIVE_LOCAL" and self.receipt_at > self.decision_at
        ):
            raise ValueError("receipt must follow completion and precede prospective decision")
        return self


class FiveMinuteLabel(LearnerModel):
    features: FiveMinuteFeatureRow
    raw_return: Finite
    benchmark_return: Finite
    excess_return: Finite
    available_at: datetime
    source_snapshot_hash: Text
    evaluation_class: EvidenceClass

    @model_validator(mode="after")
    def check(self) -> Self:
        _aware(self.available_at)
        if self.available_at < self.features.target_at:
            raise ValueError("label cannot precede target")
        if self.excess_return != self.raw_return - self.benchmark_return:
            raise ValueError("excess return must subtract benchmark")
        if self.evaluation_class != self.features.evaluation_class:
            raise ValueError("label evidence must match features")
        return self


def _index(bars: Sequence[Bar]) -> dict[tuple[str, datetime], Bar]:
    result = {}
    for bar in bars:
        stamp = _aware(bar.timestamp)
        if stamp.minute % 5 or stamp.second or stamp.microsecond:
            raise ValueError("five-minute bars must have aligned interval-start timestamps")
        key = (bar.symbol, stamp)
        if key in result:
            raise ValueError("duplicate symbol/five-minute bar")
        result[key] = bar
    return result


def _values(asset: Sequence[Bar], market: Sequence[Bar]) -> tuple[float, ...]:
    a = tuple(float(b.close) for b in asset)
    m = tuple(float(b.close) for b in market)
    r5, r15, m15 = a[-1] / a[-2] - 1, a[-1] / a[-4] - 1, m[-1] / m[-4] - 1

    def vol(p: tuple[float, ...]) -> float:
        return statistics.pstdev(y / x - 1 for x, y in zip(p[-4:-1], p[-3:], strict=True))

    return (
        r5,
        r15,
        a[-1] / a[-7] - 1,
        vol(a),
        r15 - m15,
        a[-1] / statistics.fmean(a[-3:]) - 1,
        a[-1] / statistics.fmean(a[-6:]) - 1,
        r5 - r15 / 3,
        m15,
        vol(m),
    )


def _build_features(
    bars: Sequence[Bar],
    calendar: XNYSSessionSequence,
    *,
    decision_at: datetime,
    symbols: tuple[str, ...],
    source_snapshot_hash: str,
    receipt_at: datetime,
    evaluation_class: EvidenceClass,
) -> tuple[FiveMinuteFeatureRow, ...]:
    decision_at, receipt_at = _aware(decision_at), _aware(receipt_at)
    if evaluation_class == "PROSPECTIVE_LOCAL" and receipt_at > decision_at:
        raise ValueError("receipt cannot follow prospective decision")
    if (
        not symbols
        or len(set(symbols)) != len(symbols)
        or any(not s or s != s.strip().upper() or s == "SPY" for s in symbols)
    ):
        raise ValueError("symbols must be unique uppercase assets excluding SPY")
    indexed = _index(bars)
    session = next((s for s in calendar.sessions if s.open_at <= decision_at < s.close_at), None)
    if session is None:
        return ()
    required = (*sorted(symbols), "SPY")
    eligible = [
        stamp
        for symbol, stamp in indexed
        if symbol in required and session.open_at <= stamp and stamp + INTERVAL <= decision_at
    ]
    if not eligible:
        return ()
    last = max(eligible)
    cutoff = last + INTERVAL
    if decision_at - cutoff > timedelta(minutes=2) or cutoff + HORIZON > session.close_at:
        return ()
    first = last - HORIZON
    if first < session.open_at:
        return ()
    stamps = tuple(first + i * INTERVAL for i in range(7))
    if any((s, t) not in indexed for s in required for t in stamps):
        return ()
    benchmark = tuple(indexed["SPY", t] for t in stamps)
    return tuple(
        FiveMinuteFeatureRow(
            symbol=s,
            decision_at=decision_at,
            feature_at=cutoff,
            target_at=cutoff + HORIZON,
            values=_values(tuple(indexed[s, t] for t in stamps), benchmark),
            reference_close=float(indexed[s, last].close),
            benchmark_close=float(benchmark[-1].close),
            source_snapshot_hash=source_snapshot_hash,
            receipt_at=receipt_at,
            evaluation_class=evaluation_class,
        )
        for s in sorted(symbols)
    )


def build_features(
    bars: Sequence[Bar],
    calendar: XNYSSessionSequence,
    *,
    decision_at: datetime,
    symbols: tuple[str, ...],
    source_snapshot_hash: str,
    receipt_at: datetime,
) -> tuple[FiveMinuteFeatureRow, ...]:
    return _build_features(
        bars,
        calendar,
        decision_at=decision_at,
        symbols=symbols,
        source_snapshot_hash=source_snapshot_hash,
        receipt_at=receipt_at,
        evaluation_class="PROSPECTIVE_LOCAL",
    )


def build_historical_labels(
    bars: Sequence[Bar],
    calendar: XNYSSessionSequence,
    *,
    decision_times: Sequence[datetime],
    symbols: tuple[str, ...],
    source_snapshot_hash: str,
    observed_at: datetime,
) -> tuple[FiveMinuteLabel, ...]:
    """Exploratory reconstruction retaining today's true availability, not forecasts."""
    observed_at = _aware(observed_at)
    times = tuple(_aware(t) for t in decision_times)
    if len(set(times)) != len(times):
        raise ValueError("duplicate historical decision time")
    _index(bars)  # Reject malformed/duplicate source even outside requested windows.
    by_date: dict[object, list[Bar]] = {}
    for bar in bars:
        by_date.setdefault(_aware(bar.timestamp).date(), []).append(bar)
    result = []
    for decision in sorted(times):
        if decision > observed_at:
            raise ValueError("historical decisions must already be observed")
        panel = by_date.get(decision.date(), [])
        rows = _build_features(
            panel,
            calendar,
            decision_at=decision,
            symbols=symbols,
            source_snapshot_hash=source_snapshot_hash,
            receipt_at=observed_at,
            evaluation_class="EXPLORATORY_HISTORICAL",
        )
        for row in rows:
            label = resolve_label(
                row,
                panel,
                resolved_at=observed_at,
                receipt_at=observed_at,
                source_snapshot_hash=source_snapshot_hash,
            )
            if label is not None:
                result.append(label)
    return tuple(result)


def resolve_label(
    features: FiveMinuteFeatureRow,
    bars: Sequence[Bar],
    *,
    resolved_at: datetime,
    receipt_at: datetime,
    source_snapshot_hash: str,
) -> FiveMinuteLabel | None:
    if not isinstance(features, FiveMinuteFeatureRow) or features.contract != CONTRACT:
        raise ValueError("five-minute contract required; minute models cannot be reused")
    resolved_at, receipt_at = _aware(resolved_at), _aware(receipt_at)
    if not features.target_at <= receipt_at <= resolved_at:
        return None
    indexed = _index(bars)
    start = features.target_at - INTERVAL
    asset, market = indexed.get((features.symbol, start)), indexed.get(("SPY", start))
    if asset is None or market is None:
        return None
    raw, benchmark = (
        float(asset.close) / features.reference_close - 1,
        float(market.close) / features.benchmark_close - 1,
    )
    return FiveMinuteLabel(
        features=features,
        raw_return=raw,
        benchmark_return=benchmark,
        excess_return=raw - benchmark,
        available_at=receipt_at,
        source_snapshot_hash=source_snapshot_hash,
        evaluation_class=features.evaluation_class,
    )
