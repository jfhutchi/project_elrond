"""Bounded stored-price replay. No receipt/membership vintage means no SHADOW authority."""

from __future__ import annotations

import json
from datetime import date, timedelta

from pydantic import Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from quantbot.domain import Bar
from quantbot.learner import service
from quantbot.learner.data import MAX_LOOKBACK, DailyInput, Dataset, build_dataset
from quantbot.learner.models import EvaluationClass, LearnerModel
from quantbot.learner.repository import LearnerRepository
from quantbot.research.manifest import content_id
from quantbot.research.registry import DataRole, DataWindow
from quantbot.storage import Database
from quantbot.storage.schema import bars
from quantbot.strategy import XNYSSessionSequence

PROVIDER = "alpaca:iex:all:1day"
OBSERVATIONS = "us-equities-daily"
LIMITATIONS = (
    "Historical fixed-roster ETF replay; historical membership and receipt times absent. "
    "Availability at close is assumed, not authenticated. All-adjusted IEX bars are a "
    "single-venue current vintage; corporate-action revision and survivorship bias remain. "
    "Previously explored windows are not untouched evidence. No capacity or promotion authority."
)


class StoreReplaySpec(LearnerModel):
    sessions: XNYSSessionSequence
    symbols: tuple[str, ...] = Field(min_length=2)
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    holdout_start: date
    holdout_end: date

    @field_validator("symbols")
    @classmethod
    def canonical_roster(cls, symbols: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(symbols)) != len(symbols):
            raise ValueError("duplicate universe symbol")
        return tuple(sorted(symbols))


def load_inputs(
    session: Session, calendar: XNYSSessionSequence, symbols: tuple[str, ...],
    start: int, end: int,
) -> tuple[DailyInput, ...]:
    """Guard before SQL reads, then demand one complete, exactly aligned daily panel.

    Use on a dedicated research snapshot retaining the source protection registry. The
    bounded SELECT never materializes bars from dates outside the requested interval.
    """
    if not 0 <= start <= end < len(calendar.sessions):
        raise ValueError("invalid calendar range")
    if not symbols or len(set(symbols)) != len(symbols) or any(
        s != s.upper().strip() for s in symbols
    ):
        raise ValueError("symbols must be unique normalized uppercase names")
    selected = calendar.sessions[start:end + 1]
    service.guard_range(session, OBSERVATIONS, selected[0].session_date, selected[-1].session_date)
    roster = set(symbols) | {"SPY"}
    rows = session.execute(select(bars).where(
        bars.c.provider == PROVIDER,
        bars.c.symbol.in_(sorted(roster)),
        bars.c.timestamp >= selected[0].session_date.isoformat(),
        bars.c.timestamp < (selected[-1].session_date + timedelta(days=1)).isoformat(),
    ).order_by(bars.c.timestamp, bars.c.symbol)).mappings()
    result: dict[tuple[str, object], DailyInput] = {}
    closes = {s.close_at for s in selected}
    for row in rows:
        metadata = json.loads(row["adjustment_metadata_json"])
        if metadata != {"provider": "alpaca", "feed": "iex",
                        "adjustment": "all", "timeframe": "1Day"}:
            raise ValueError("stored bar provenance does not match the replay source contract")
        bar = Bar(**{name: row[name] for name in (
            "symbol", "timestamp", "open", "high", "low", "close", "volume", "adjustment"
        )})
        if bar.timestamp not in closes:
            raise ValueError("stored bar is not aligned to the supplied calendar")
        key = (bar.symbol, bar.timestamp)
        if key in result:
            raise ValueError("duplicate stored bar provenance")
        result[key] = DailyInput(bar=bar, available_at=bar.timestamp)
    if set(result) != {(s, t) for s in roster for t in closes}:
        raise ValueError("stored replay requires a complete panel; missing sessions are not filled")
    return tuple(result.values())


def build_from_store(database: Database, spec: StoreReplaySpec) -> Dataset:
    dates = [s.session_date for s in spec.sessions.sessions]
    first = dates.index(spec.train_start) - MAX_LOOKBACK
    last = dates.index(spec.validation_end)
    # Validate declared partitions before reading their prices, including the new holdout.
    if not (spec.train_start <= spec.train_end < spec.validation_start
            <= spec.validation_end < spec.holdout_start <= spec.holdout_end):
        raise ValueError("chronological partitions must not overlap")
    with database.transaction() as session:
        inputs = load_inputs(session, spec.sessions, spec.symbols, first, last)
        snapshot = content_id({"provider": PROVIDER, "spec": spec, "inputs": inputs})
        windows = [DataWindow(
            dataset=OBSERVATIONS, snapshot=snapshot, role=role,
            start=getattr(spec, name + "_start"), end=getattr(spec, name + "_end"),
        ) for name, role in (("train", DataRole.DISCOVERY), ("validation", DataRole.VALIDATION),
                             ("holdout", DataRole.PROTECTED_EVALUATION))]
        dataset = build_dataset(
            inputs, spec.sessions, symbols=spec.symbols,
            universe_id="fixed-roster:" + content_id(sorted(spec.symbols)),
            source_id="stored-replay:" + snapshot, train_window=windows[0],
            validation_window=windows[1], holdout_window=windows[2],
        )
        LearnerRepository(session).save_dataset(dataset)
    return dataset


def verify_source(
    session: Session, dataset: Dataset, calendar: XNYSSessionSequence,
) -> tuple[DailyInput, ...]:
    """A restored or edited store must not silently change an older model's input vintage."""
    spec = StoreReplaySpec(
        sessions=calendar, symbols=dataset.symbols,
        train_start=dataset.train_window.start, train_end=dataset.train_window.end,
        validation_start=dataset.validation_window.start,
        validation_end=dataset.validation_window.end,
        holdout_start=dataset.holdout_window.start, holdout_end=dataset.holdout_window.end,
    )
    dates = [s.session_date for s in calendar.sessions]
    inputs = load_inputs(session, calendar, dataset.symbols,
                         dates.index(spec.train_start) - MAX_LOOKBACK,
                         dates.index(spec.validation_end))
    snapshot = content_id({"provider": PROVIDER, "spec": spec, "inputs": inputs})
    if dataset.source_id != "stored-replay:" + snapshot:
        raise ValueError("stored prices or calendar differ from the immutable source snapshot")
    return inputs


def resolve_from_store(
    database: Database, model_id: str, calendar: XNYSSessionSequence,
) -> int:
    with database.transaction() as session:
        repository = LearnerRepository(session)
        dataset = repository.get_dataset(repository.get_model(model_id).dataset_id)
        if not dataset.source_id.startswith("stored-replay:"):
            raise ValueError("model was not trained from the stored replay source")
        verified_inputs = verify_source(session, dataset, calendar)
        predictions = repository.list_predictions(model_id)
        if any(p.evaluation_class is not EvaluationClass.HISTORICAL_REPLAY for p in predictions):
            raise ValueError("stored replay cannot resolve prospective or SHADOW evidence")
        targets = {p.features.target_at for p in predictions
                   if p.features.target_at <= service._utcnow()
                   and repository.get_outcome(p.prediction_id) is None}
        inputs = tuple(r for r in verified_inputs if r.bar.timestamp in targets)
    return len(service.resolve(database, model_id, inputs, source_id=dataset.source_id))
