"""Isolated observational five-minute producer. No fit, order, or deployment API."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from quantbot.config import Settings
from quantbot.domain import Bar
from quantbot.learner.estimators import FittedEstimator, predict
from quantbot.learner.five_minute import (
    CONTRACT,
    FEATURE_NAMES,
    FiveMinuteFeatureRow,
    build_features,
    resolve_label,
)
from quantbot.learner.forward import _read, _save
from quantbot.learner.forward_source import collect_snapshot
from quantbot.learner.intraday import Finite, Text, _aware
from quantbot.learner.models import LearnerModel
from quantbot.research.manifest import content_id
from quantbot.strategy import XNYSSessionSequence


class FiveMinutePrediction(LearnerModel):
    model_id: Text = Field(pattern=r"^[0-9a-f]{64}$")
    features: FiveMinuteFeatureRow
    recorded_at: datetime
    score: Finite
    evaluation_class: Literal["PROSPECTIVE_LOCAL"] = "PROSPECTIVE_LOCAL"
    fixed_cost_floor: Finite = Field(default=0.001, ge=0.001, le=0.001)
    cost_status: Literal["ABOVE_FIXED_COST_FLOOR", "BELOW_FIXED_COST_FLOOR"]
    order_authority: Literal[False] = False

    @property
    def prediction_id(self) -> str:
        """Original observation identity; adding an adapter does not rewrite evidence."""
        return content_id(self)

    @model_validator(mode="after")
    def verify_forward_record(self) -> Self:
        recorded = _aware(self.recorded_at)
        if self.features.evaluation_class != "PROSPECTIVE_LOCAL" or not (
            self.features.receipt_at
            <= self.features.decision_at
            <= recorded
            < self.features.target_at
        ):
            raise ValueError("prospective receipt and recording must precede target")
        expected = "ABOVE_FIXED_COST_FLOOR" if self.score > 0.001 else "BELOW_FIXED_COST_FLOOR"
        if self.cost_status != expected:
            raise ValueError("cost status must match unchanged fixed threshold")
        return self


def forecast(
    model: dict[str, Any],
    identity: str,
    features: Sequence[FiveMinuteFeatureRow],
    *,
    recorded_at: datetime,
) -> tuple[FiveMinutePrediction, ...]:
    recorded_at = _aware(recorded_at)
    if (
        content_id(model) != identity
        or model.get("contract") != CONTRACT
        or tuple(model["feature_names"]) != FEATURE_NAMES
        or tuple(sorted({r.symbol for r in features})) != tuple(model["training_symbols"])
    ):
        raise ValueError("frozen five-minute contract, hash and roster required")
    created_at = _aware(datetime.fromisoformat(model["created_at"]))
    cohorts = {
        (r.decision_at, r.feature_at, r.target_at, r.source_snapshot_hash, r.receipt_at)
        for r in features
    }
    if len(cohorts) > 1 or len({r.symbol for r in features}) != len(features):
        raise ValueError("one complete unique prospective cohort required")
    for row in features:
        if (
            row.evaluation_class != "PROSPECTIVE_LOCAL"
            or created_at > row.decision_at
            or not row.receipt_at <= row.decision_at <= recorded_at < row.target_at
        ):
            raise ValueError("model and receipt must precede real forward decision and recording")
    artifact = FittedEstimator.model_validate(model["artifact"])
    if artifact.feature_names != FEATURE_NAMES:
        raise ValueError("estimator must use the separately frozen five-minute features")
    scores = predict(artifact, tuple(r.values for r in features))
    return tuple(
        FiveMinutePrediction(
            model_id=identity,
            features=row,
            recorded_at=recorded_at,
            score=score,
            cost_status="ABOVE_FIXED_COST_FLOOR" if score > 0.001 else "BELOW_FIXED_COST_FLOOR",
        )
        for row, score in zip(features, scores, strict=True)
    )


def window_wait(bars: Sequence[Bar], calendar: XNYSSessionSequence, decision: datetime) -> str:
    decision = _aware(decision)
    session = next((s for s in calendar.sessions if s.open_at <= decision < s.close_at), None)
    if session is None:
        return "OUTSIDE_FORECAST_SESSION"
    cutoff = decision.replace(minute=(decision.minute // 5) * 5, second=0, microsecond=0)
    if cutoff - session.open_at < timedelta(minutes=35):
        return "FIVE_MINUTE_LOOKBACK_WARMUP"
    if cutoff + timedelta(minutes=30) > session.close_at:
        return "FORECAST_HORIZON_CLOSED"
    keys = {(b.symbol, _aware(b.timestamp)) for b in bars}
    if any(
        (symbol, cutoff - timedelta(minutes=35 - 5 * i)) not in keys
        for symbol in ("DIA", "SPY")
        for i in range(7)
    ):
        return "INCOMPLETE_FIVE_MINUTE_PANEL"
    if decision - cutoff > timedelta(minutes=2):
        return "WAITING_FOR_NEXT_FIVE_MINUTE_BOUNDARY"
    raise ValueError("complete fresh window unexpectedly rejected by feature engine")


async def observe_tick(
    root: Path,
    settings: Settings,
    models: Sequence[dict[str, Any]],
    *,
    guard: Callable[[datetime, datetime], bool],
    clock_health: Callable[[datetime], bool],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Capture now, append immutable forecasts, resolve only genuinely elapsed targets.

    The caller supplies existing declaration/registry and verified clock gates.
    Every write stays under the separately configured observation root. Pending
    targets do not acquire paper order authority; no execution module is called.
    """
    started = _aware(clock())
    if (
        settings.TRADING_MODE.value != "PAPER"
        or settings.BROKER_ENVIRONMENT.value != "PAPER"
        or settings.LIVE_TRADING_ACKNOWLEDGED
    ):
        raise ValueError("observation integration requires existing PAPER-only context")
    files = sorted((root / "predictions").glob("*/*.json"))
    if len(files) > 10000:
        raise ValueError("observation archive requires bounded maintenance")
    pending = []
    for path in files:
        if (root / "outcomes" / path.parent.name / path.name).exists():
            continue
        prediction = FiveMinutePrediction.model_validate(_read(path))
        pending.append((path, prediction))
    start = min([started.date()] + [p.features.feature_at.date() for _, p in pending])
    if (started.date() - start).days > 31:
        raise ValueError("old pending outcomes require explicit bounded recovery")
    begin = datetime.combine(start, time.min, UTC)
    end = datetime.combine(started.date(), time.max, UTC)
    if not guard(begin, end):
        return dict(status="PROTECTED_OBSERVATION_WINDOW", orders=0, predictions=0, outcomes=0)
    if not clock_health(started):
        return dict(status="WAITING_FOR_VERIFIED_CLOCK", orders=0, predictions=0, outcomes=0)
    snapshot = await collect_snapshot(
        root, ("DIA",), start, started.date(), settings, timeframe="5Min", clock=clock
    )
    decision = _aware(clock())
    if not guard(begin, end) or not clock_health(decision):
        return dict(
            status="POST_CAPTURE_GUARD_OR_CLOCK_BLOCKED", orders=0, predictions=0, outcomes=0
        )
    bars = tuple(i.bar for i in snapshot.inputs)
    resolved = 0
    for path, prediction in pending:
        label = resolve_label(
            prediction.features,
            bars,
            resolved_at=decision,
            receipt_at=snapshot.received_at,
            source_snapshot_hash=snapshot.identity,
        )
        if label is None:
            continue
        _save(
            root / "outcomes" / path.parent.name / path.name,
            dict(
                prediction_id=path.stem,
                prediction_hash=content_id(prediction),
                label=label.model_dump(mode="json"),
                evidence_class="PROSPECTIVE_LOCAL",
                orders=0,
                account_pnl=False,
            ),
        )
        resolved += 1
    rows = build_features(
        bars,
        snapshot.sessions,
        decision_at=decision,
        symbols=("DIA",),
        source_snapshot_hash=snapshot.identity,
        receipt_at=snapshot.received_at,
    )
    published = 0
    if rows:
        for model in models:
            identity = content_id(model)
            for prediction in forecast(model, identity, rows, recorded_at=_aware(clock())):
                folder = prediction.features.feature_at.strftime("%Y-%m-%d")
                name = prediction.features.feature_at.strftime("%H%M") + "-" + identity
                path = root / "predictions" / folder / (name + ".json")
                if path.exists():
                    # Existing decisions are immutable, never regenerated from later bars.
                    old = FiveMinutePrediction.model_validate(_read(path))
                    if (
                        old.model_id != identity
                        or old.features.feature_at != prediction.features.feature_at
                    ):
                        raise ValueError("observation archive identity conflict")
                    continue
                _save(path, prediction.model_dump(mode="json"))
                published += 1
    return dict(
        status="OBSERVING" if rows else window_wait(bars, snapshot.sessions, decision),
        captured_at=snapshot.received_at.isoformat(),
        source_snapshot=snapshot.identity,
        captured_bars=len(bars),
        predictions=published,
        outcomes=resolved,
        pending=len(pending) - resolved + published,
        feature_cutoff=rows[0].feature_at.isoformat() if rows else None,
        target_at=rows[0].target_at.isoformat() if rows else None,
        orders=0,
        trained=0,
        evaluation_class="PROSPECTIVE_LOCAL",
        account_pnl=False,
        order_authority=False,
    )
