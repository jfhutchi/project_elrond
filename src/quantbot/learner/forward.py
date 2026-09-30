"""Durable intraday observations, prospective forecasts, outcomes and bounded challengers.

Capture archives are not research access grants. The canonical learner range guard runs
before feature, outcome or training reads. This module never constructs a broker submitter.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from quantbot.config import Settings
from quantbot.learner import service
from quantbot.learner.forward_metrics import score_cohort, summarize
from quantbot.learner.forward_source import Snapshot, collect_snapshot
from quantbot.learner.intraday import (
    IntradayFeatureRow,
    IntradayLabel,
    IntradayModel,
    IntradayPrediction,
    build_historical_labels,
    fit_intraday_model,
    predict_intraday,
    resolve_label,
)
from quantbot.learner.intraday import build_features as intraday_features
from quantbot.market_data.calendar import XNYS_TIMEZONE
from quantbot.research.intraday_accounting import record_intraday_attempt
from quantbot.research.manifest import content_id
from quantbot.research.registry import HypothesisRegistry
from quantbot.storage import Database
from quantbot.storage.schema import search_runs


class ForwardConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    symbols: tuple[str, ...] = ("IWM", "QQQ")
    minimum_training_days: int = Field(default=20, ge=20)
    validation_days: int = Field(default=5, ge=5)
    retrain_after_cohorts: int = Field(default=24, ge=24)
    retrain_cooldown_hours: int = Field(default=4, ge=4)
    registry_database: Path | None = None


def _ensure_contract(root: Path, config: ForwardConfig) -> None:
    # A different universe or training policy needs an explicitly separate experiment.
    _save(root / "contract.json", config.model_dump(mode="json", exclude={"registry_database"}))


_REGISTRY: ContextVar[Path | None] = ContextVar("intraday_registry", default=None)


@contextmanager
def _registry_session(*, write: bool = False) -> Iterator[Session]:
    path = _REGISTRY.get()
    if path is None or not path.is_absolute() or not path.is_file():
        raise ValueError("existing absolute research registry required")
    uri = path.resolve().as_uri() + ("?mode=rw" if write else "?mode=ro")
    engine = create_engine("sqlite+pysqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
    try:
        with Session(engine) as session, session.begin():
            yield session
    finally:
        engine.dispose()


def _now() -> datetime:
    return datetime.now(UTC)


def _read(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 32_000_000:
        raise ValueError("forward record exceeds limit")
    record = json.loads(path.read_text(encoding="utf-8"))
    if content_id(record["document"]) != record["hash"]:
        raise ValueError("forward record integrity failure")
    return dict(record["document"])


def _save(path: Path, document: dict[str, Any]) -> None:
    """Publish once without replacing a prior cohort, outcome, model or attempt."""
    envelope = dict(hash=content_id(document), document=document)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            output.write(json.dumps(envelope, sort_keys=True, separators=(",", ":")))
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _read(path) != document:
                raise ValueError("immutable forward record conflict") from None
    finally:
        temporary.unlink(missing_ok=True)


def _guard(database: Database, start: datetime, end: datetime) -> bool:
    if _REGISTRY.get() is not None:
        with _registry_session() as session:
            if HypothesisRegistry(session).blocking_protected_windows(
                "us-equities-daily",
                start.date(),
                end.date(),
                now=_now().date(),
                include_forward_paper=True,
            ):
                return False
    with database.transaction() as session:
        try:
            service.guard_range(session, "us-equities-daily", start.date(), end.date())
        except ValueError:
            return False
    return True


def _eligible_files(database: Database, root: Path, section: str) -> list[Path]:
    paths: list[Path] = []
    for folder in sorted((root / section).glob("*")):
        day = date.fromisoformat(folder.name)
        start = datetime.combine(day, datetime.min.time(), UTC)
        if _guard(database, start, start):
            paths.extend(sorted(folder.glob("*.json")))
    return paths


def _models(root: Path) -> list[IntradayModel]:
    models = []
    published = {
        identity for path in (root / "fits").glob("*.json") for identity in _read(path)["model_ids"]
    }
    for identity in published:
        if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("invalid published model identity")
        path = root / "models" / (identity + ".json")
        model = IntradayModel.model_validate(_read(path))
        if path.stem != model.model_id:
            raise ValueError("model filename identity mismatch")
        models.append(model)
    return sorted(models, key=lambda m: m.created_at)


def _forecast_models(root: Path) -> list[IntradayModel]:
    models = _models(root)
    selected = []
    for family in ("mean", "ridge"):
        candidates = [m for m in models if m.family == family]
        if candidates:
            selected.append(candidates[0])
            if family == "ridge" and candidates[-1].model_id != candidates[0].model_id:
                selected.append(candidates[-1])
    return selected


def _train_challengers(
    root: Path,
    database: Database,
    config: ForwardConfig,
    code_commit: str,
) -> tuple[int, str]:
    _ensure_contract(root, config)
    completed = {
        identity
        for path in (root / "fits").glob("*.json")
        for identity in _read(path)["search_ids"]
    }
    with database.transaction() as session:
        attempts = set(
            session.scalars(
                select(search_runs.c.search_id).where(
                    search_runs.c.search_id.like("intraday:learner-%")
                )
            )
        )
    if _REGISTRY.get() is not None:
        with _registry_session() as session:
            attempts.update(
                session.scalars(
                    select(search_runs.c.search_id).where(
                        search_runs.c.search_id.like("intraday:learner-%")
                    )
                )
            )
    if attempts - completed:
        # New data must not disguise a failed fit as a fresh ten-minute search attempt.
        return 0, "TRAINING_ATTEMPT_REQUIRES_REVIEW"
    labels = [
        IntradayLabel.model_validate(item)
        for path in _eligible_files(database, root, "outcomes")
        for item in _read(path)["labels"]
    ]
    days = sorted({label.features.decision_at.date() for label in labels})
    if len(days) < config.minimum_training_days + config.validation_days and not _models(root):
        labels = [
            IntradayLabel.model_validate(item)
            for path in _eligible_files(database, root, "training_history")
            for item in _read(path)["labels"]
        ]
        days = sorted({label.features.decision_at.date() for label in labels})
    if len(days) < config.minimum_training_days + config.validation_days:
        return 0, "TRAINING_DATA_WARMUP"
    validation_start = datetime.combine(days[-config.validation_days], datetime.min.time(), UTC)
    if not _guard(
        database,
        min(label.features.feature_at for label in labels) - timedelta(minutes=31),
        max(label.features.target_at for label in labels),
    ):
        return 0, "PROTECTED_TRAINING_WINDOW"
    models = _models(root)
    now = _now()
    if models:
        last = models[-1]
        previous_labels = (
            {
                identity
                for path in (root / "fits").glob("*.json")
                for identity in _read(path).get("considered_label_ids", [])
            }
            | set(last.training_label_ids)
            | set(last.validation_label_ids)
        )
        new_cohorts = {
            label.features.decision_at for label in labels if label.label_id not in previous_labels
        }
        if (
            now - last.created_at < timedelta(hours=config.retrain_cooldown_hours)
            or len(new_cohorts) < config.retrain_after_cohorts
        ):
            return 0, "WAITING_FOR_NEW_TRAINING_OUTCOMES"
    frozen = content_id(
        dict(
            labels=[label.model_dump(mode="json") for label in labels],
            validation_start=validation_start.isoformat(),
            contract="minute-alpha-v1",
            config=config.model_dump(mode="json"),
            code_commit=code_commit,
        )
    )
    count = 0
    search_ids = [
        "intraday:learner-" + content_id([frozen, family]) for family in ("mean", "ridge")
    ]
    if _REGISTRY.get() is not None:
        # The active research registry receives the permanent budget charge first.
        with _registry_session(write=True) as session:
            for family, search_id in zip(("mean", "ridge"), search_ids, strict=True):
                record_intraday_attempt(
                    session,
                    search_id=search_id,
                    configuration_hash=content_id([frozen, family]),
                    start=days[0],
                    end=days[-1],
                    now=now,
                )
    with database.transaction() as session:
        # Freeze/charge the baseline-challenger pair before executing either candidate.
        for family, search_id in zip(("mean", "ridge"), search_ids, strict=True):
            record_intraday_attempt(
                session,
                search_id=search_id,
                configuration_hash=content_id([frozen, family]),
                start=days[0],
                end=days[-1],
                now=now,
            )
    model_ids = []
    for family in ("mean", "ridge"):
        parent = next((m for m in reversed(models) if m.family == family), None)
        model = fit_intraday_model(
            labels,
            validation_start=validation_start,
            created_at=_now(),
            code_commit=code_commit,
            family=family,
            parent_model_id=parent.model_id if parent else None,
        )
        _save(root / "models" / (model.model_id + ".json"), model.model_dump(mode="json"))
        model_ids.append(model.model_id)
        count += 1
    _save(
        root / "fits" / (frozen + ".json"),
        dict(
            model_ids=model_ids,
            search_ids=search_ids,
            considered_label_ids=[label.label_id for label in labels],
        ),
    )
    return count, "CHALLENGER_TRAINED_NOT_PROMOTED"


async def bootstrap_history(
    directory: Path,
    database: Database,
    settings: Settings,
    *,
    code_commit: str,
    start: date,
    end: date,
) -> dict[str, Any]:
    """One explicitly requested historical seed; never counts as forward observations."""
    config = ForwardConfig.model_validate_json(
        (directory / "forward.json").read_text(encoding="utf-8-sig")
    )
    if config.registry_database is None:
        raise ValueError("current research registry required")
    token = _REGISTRY.set(config.registry_database)
    try:
        _ensure_contract(directory / "intraday", config)
        if not _guard(
            database,
            datetime.combine(start, datetime.min.time(), UTC),
            datetime.combine(end, datetime.min.time(), UTC),
        ):
            raise ValueError("protected bootstrap range")
        snapshot = await collect_snapshot(
            directory, config.symbols, start, end, settings, timeframe="1Min"
        )
        times = []
        for session in snapshot.sessions.sessions:
            if not start <= session.session_date <= end:
                continue
            decision = session.open_at + timedelta(minutes=40)
            while decision + timedelta(minutes=30) <= session.close_at:
                times.append(decision)
                decision += timedelta(minutes=10)
        labels = build_historical_labels(
            [item.bar for item in snapshot.inputs],
            snapshot.sessions,
            decision_times=times,
            symbols=config.symbols,
            source_snapshot_hash=snapshot.identity,
            observed_at=snapshot.received_at,
        )
        groups: dict[datetime, list[IntradayLabel]] = {}
        for label in labels:
            groups.setdefault(label.features.decision_at, []).append(label)
        for decision, group in groups.items():
            if {label.features.symbol for label in group} == set(config.symbols):
                _save(
                    directory
                    / "intraday"
                    / "training_history"
                    / decision.date().isoformat()
                    / (decision.strftime("%H%M") + ".json"),
                    dict(labels=[label.model_dump(mode="json") for label in group]),
                )
        trained, status = _train_challengers(directory / "intraday", database, config, code_commit)
        return dict(
            status=status,
            trained=trained,
            captured_bars=len(snapshot.inputs),
            historical_only=True,
            source_snapshot=snapshot.identity,
        )
    finally:
        _REGISTRY.reset(token)


def process_snapshot(
    directory: Path,
    database: Database,
    snapshot: Snapshot,
    *,
    code_commit: str,
    config: ForwardConfig,
    issue_forecasts: bool = True,
) -> dict[str, Any]:
    root = directory / "intraday"
    _ensure_contract(root, config)
    now = _now()
    result: dict[str, Any] = dict(
        mode="INTRADAY_30_MINUTE",
        captured_at=snapshot.received_at.isoformat(),
        captured_bars=len(snapshot.inputs),
        source_snapshot=snapshot.identity,
        observations_this_run=0,
        predictions_this_run=0,
        outcomes_this_run=0,
        trained_this_run=0,
        trading_enabled=False,
        progress_expected=False,
        progress_session=None,
        blockers=["PAPER_PROMOTION_REQUIRED", "PAPER_EXECUTION_NOT_CONFIGURED"],
    )
    # Every pending observation was durably recorded before its outcome. No gap forecasts.
    for path in _eligible_files(database, root, "observations"):
        output = root / "outcomes" / path.parent.name / path.name
        if output.exists():
            continue
        features = tuple(
            IntradayFeatureRow.model_validate(item) for item in _read(path)["features"]
        )
        if not features or max(f.target_at for f in features) > now:
            continue
        if not _guard(
            database,
            min(f.feature_at for f in features) - timedelta(minutes=31),
            max(f.target_at for f in features),
        ):
            continue
        labels = [
            resolve_label(
                f,
                [item.bar for item in snapshot.inputs],
                resolved_at=now,
                receipt_at=snapshot.received_at,
                source_snapshot_hash=snapshot.identity,
            )
            for f in features
        ]
        if all(label is not None for label in labels):
            _save(
                output,
                dict(
                    labels=[label.model_dump(mode="json") for label in labels if label is not None]
                ),
            )
            result["outcomes_this_run"] += len(labels)

    # Score only actual model-bound forecasts, independently of training observations.
    for path in _eligible_files(database, root, "predictions"):
        score_path = root / "scores" / path.parent.name / path.name
        if score_path.exists():
            continue
        outcome_path = root / "outcomes" / path.parent.name / (path.stem.split("-", 1)[0] + ".json")
        if not outcome_path.exists():
            continue
        predictions = [IntradayPrediction.model_validate(p) for p in _read(path)["predictions"]]
        outcomes = [IntradayLabel.model_validate(item) for item in _read(outcome_path)["labels"]]
        if not _guard(
            database,
            min(p.features.feature_at for p in predictions) - timedelta(minutes=31),
            max(p.features.target_at for p in predictions),
        ):
            continue
        report = score_cohort(
            predictions,
            outcomes,
            [item.bar for item in snapshot.inputs],
            source_snapshot_hash=snapshot.identity,
        )
        if report is not None:
            _save(score_path, report)

    session = next(
        (s for s in snapshot.sessions.sessions if s.open_at <= now <= s.close_at), None
    )
    if not issue_forecasts:
        pass  # Recovery resolves old, authentic cohorts without creating gap forecasts.
    elif session is None or now + timedelta(minutes=30) >= session.close_at:
        result["blockers"].append("OUTSIDE_FORECAST_SESSION")
    elif not _guard(database, now - timedelta(minutes=32), now + timedelta(minutes=30)):
        result["blockers"].append("PROTECTED_FEATURE_WINDOW")
    elif now < session.open_at + timedelta(minutes=31):
        result["blockers"].append("FORECAST_LOOKBACK_WARMUP")
    else:
        result['progress_expected'] = True
        result['progress_session'] = session.session_date.isoformat()
        slot = now.replace(minute=now.minute // 10 * 10, second=0, microsecond=0)
        path = root / "observations" / slot.date().isoformat() / (slot.strftime("%H%M") + ".json")
        if not path.exists():
            try:
                features = intraday_features(
                    [item.bar for item in snapshot.inputs],
                    snapshot.sessions,
                    decision_at=now,
                    symbols=config.symbols,
                    source_snapshot_hash=snapshot.identity,
                    receipt_at=snapshot.received_at,
                )
                if not features:
                    raise ValueError("no complete minute cohort")
            except ValueError:
                result["blockers"].append("INCOMPLETE_OR_STALE_MINUTE_DATA")
            else:
                _save(path, dict(features=[f.model_dump(mode="json") for f in features]))
                result["observations_this_run"] = len(features)
                for model in _forecast_models(root):
                    issued = predict_intraday(model, features, recorded_at=_now())
                    target = (
                        root
                        / "predictions"
                        / slot.date().isoformat()
                        / (slot.strftime("%H%M") + "-" + model.model_id + ".json")
                    )
                    _save(
                        target,
                        dict(
                            model_id=model.model_id,
                            predictions=[p.model_dump(mode="json") for p in issued],
                        ),
                    )
                    result["predictions_this_run"] += len(issued)
    result["trained_this_run"], training_status = _train_challengers(
        root, database, config, code_commit
    )
    result["training_status"] = training_status
    result["observation_cohorts"] = len(_eligible_files(database, root, "observations"))
    result["resolved_cohorts"] = len(_eligible_files(database, root, "outcomes"))
    result["prediction_cohorts"] = len(_eligible_files(database, root, "predictions"))
    result["model_count"] = len(_models(root))
    selected_ids = {m.model_id for m in _forecast_models(root)}
    reports = [_read(path) for path in _eligible_files(database, root, "scores")]
    result["performance"] = summarize([r for r in reports if r["model_id"] in selected_ids])
    return result


async def run_forward_cycle(
    directory: Path,
    database: Database,
    settings: Settings,
    *,
    code_commit: str,
) -> dict[str, Any]:
    config = ForwardConfig.model_validate_json(
        (directory / "forward.json").read_text(encoding="utf-8-sig")
    )
    if config.registry_database is None:
        raise ValueError("forward worker requires the current operational research registry")
    token = _REGISTRY.set(config.registry_database)
    try:
        return await _collect_and_process(directory, database, settings, code_commit, config)
    finally:
        _REGISTRY.reset(token)


async def _collect_and_process(
    directory: Path,
    database: Database,
    settings: Settings,
    code_commit: str,
    config: ForwardConfig,
) -> dict[str, Any]:
    # Refuse a missing/stale registry connection before any provider access.
    with _registry_session() as session:
        session.execute(select(search_runs.c.search_id).limit(1))
    now = _now()
    session_date = now.astimezone(XNYS_TIMEZONE).date()
    pending_days = [
        date.fromisoformat(path.parent.name)
        for path in _eligible_files(database, directory / "intraday", "observations")
        if not (directory / "intraday" / "outcomes" / path.parent.name / path.name).exists()
    ]
    start = max(min(pending_days, default=session_date), session_date - timedelta(days=7))
    recovered = 0
    recovery_fits = 0
    if pending_days and min(pending_days) < start:
        # One oldest day per pass bounds outage recovery without losing genuine forecasts.
        oldest = min(pending_days)
        historical = await collect_snapshot(
            directory, config.symbols, oldest, oldest, settings, timeframe="1Min"
        )
        recovery = process_snapshot(
            directory,
            database,
            historical,
            code_commit=code_commit,
            config=config,
            issue_forecasts=False,
        )
        recovered = recovery["outcomes_this_run"]
        recovery_fits = recovery["trained_this_run"]
    snapshot = await collect_snapshot(
        directory, config.symbols, start, session_date, settings, timeframe="1Min"
    )
    result = process_snapshot(directory, database, snapshot, code_commit=code_commit, config=config)
    result["outcomes_this_run"] += recovered
    result["trained_this_run"] += recovery_fits
    if recovery_fits and result["training_status"] == "WAITING_FOR_NEW_TRAINING_OUTCOMES":
        result["training_status"] = "CHALLENGER_TRAINED_NOT_PROMOTED"
    result["recovered_outcomes"] = recovered
    return result
