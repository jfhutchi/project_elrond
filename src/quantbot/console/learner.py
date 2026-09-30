"""Read-only learner projection, usable by a schema-28 console over a separate schema-29 store.

No learner service, broker, fit, migration or scheduler is constructed by a dashboard GET.
Saved report hashes prove local integrity, not provider authentication or worker liveness.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from quantbot.research.manifest import content_id, looks_like_credential

Hash = Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
Count = Annotated[int, Field(strict=True, ge=0)]


def _timestamp(value: str) -> str:
    if datetime.fromisoformat(value).tzinfo is None:
        raise ValueError("timestamp needs timezone")
    return value


class PanelModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    @field_validator("*", mode="after")
    @classmethod
    def safe_text(cls, value: Any) -> Any:
        if isinstance(value, str) and looks_like_credential(value):
            raise ValueError("credential-shaped display value")
        return value


class ModelRow(PanelModel):
    model_id: str
    family: Literal["mean", "ridge", "quadratic_ridge"]
    version: int
    created_at: str
    code_commit: Hash
    artifact_hash: Hash

    _valid_time = field_validator("created_at")(_timestamp)


class EquityPoint(PanelModel):
    at: str
    equity: float

    _valid_time = field_validator("at")(_timestamp)


class PerformanceRow(PanelModel):
    name: str
    model_id: str | None
    net_return: float
    max_drawdown: float
    excess_return_vs_spy: float
    cost_over_initial: float
    traded_notional_over_initial: float
    mean_daily_gross_exposure: float
    rank_ic: float | None
    periods: int
    start: str
    end: str
    daily_equity: tuple[EquityPoint, ...]

    _valid_times = field_validator("start", "end")(_timestamp)


class IntradayPerformance(PanelModel):
    model_id: Hash
    matched_periods: Count
    simulated_net_return: float | None
    simulated_max_drawdown: float | None
    rank_ic: float | None
    mse: float | None


class IntradayPanel(PanelModel):
    mode: Literal['INTRADAY_30_MINUTE']
    captured_at: str
    captured_bars: Count
    observations_this_run: Count
    predictions_this_run: Count
    outcomes_this_run: Count
    trained_this_run: Count
    observation_cohorts: Count
    resolved_cohorts: Count
    prediction_cohorts: Count
    model_count: Count
    training_status: Literal[
        'TRAINING_DATA_WARMUP', 'PROTECTED_TRAINING_WINDOW', 'WAITING_FOR_NEW_TRAINING_OUTCOMES',
        'TRAINING_ATTEMPT_REQUIRES_REVIEW', 'CHALLENGER_TRAINED_NOT_PROMOTED',
    ]
    performance: tuple[IntradayPerformance, ...] = ()

    _capture_time = field_validator('captured_at')(_timestamp)


class ActiveAlert(PanelModel):
    code: str
    subsystem: str
    severity: Literal['INFO', 'WARNING', 'CRITICAL']
    category: str
    owner: str
    action: str
    first_seen: str
    last_seen: str
    acknowledged_at: str | None
    resolved_at: None
    last_notified_at: str | None
    escalation: Count
    duration_seconds: Annotated[float, Field(ge=0)]

    _times = field_validator('first_seen', 'last_seen')(_timestamp)

    @field_validator('acknowledged_at', 'last_notified_at')
    @classmethod
    def optional_time(cls, value: str | None) -> str | None:
        return _timestamp(value) if value is not None else None


class ProgressPanel(PanelModel):
    forecast_status: str
    paper_status: str
    training_status: str
    last_progress_at: str | None
    expected_idle_seconds: Annotated[float, Field(ge=0)]
    wait_reasons: tuple[str, ...]
    active_alerts: tuple[ActiveAlert, ...]

    @field_validator('last_progress_at')
    @classmethod
    def optional_time(cls, value: str | None) -> str | None:
        return _timestamp(value) if value is not None else None


class LearnerPanel(PanelModel):
    status: str = "NOT_CONFIGURED"
    reason: str = "The console has no learner results directory configured."
    historical_predictions: int | None = None
    prospective_predictions: int | None = None
    shadow_predictions: int | None = None
    resolved_predictions: int | None = None
    pending_predictions: int | None = None
    fit_attempts: int | None = None
    last_prediction_at: str | None = None
    last_resolution_at: str | None = None
    last_evaluation_at: str | None = None
    holdout_start: str | None = None
    holdout_end: str | None = None
    models: tuple[ModelRow, ...] = ()
    performance: tuple[PerformanceRow, ...] = ()
    report_errors: tuple[str, ...] = ()
    promotion: str = "NONE"
    last_worker_at: str | None = None
    interval_minutes: int | None = None
    resolved_this_run: int | None = None
    worker_blockers: tuple[str, ...] = ()
    intraday: IntradayPanel | None = None
    progress: ProgressPanel | None = None

    @field_validator("holdout_start", "holdout_end")
    @classmethod
    def valid_date(cls, value: str | None) -> str | None:
        if value is not None:
            date.fromisoformat(value)
        return value

    @field_validator("last_prediction_at", "last_resolution_at", "last_evaluation_at")
    @classmethod
    def valid_optional_time(cls, value: str | None) -> str | None:
        return _timestamp(value) if value is not None else None

    @property
    def chart(self) -> dict[str, Any]:
        """Display coordinates only; returns come from the existing economic simulator."""
        values = [1.0] + [p.equity for r in self.performance for p in r.daily_equity]
        low, high = min(values), max(values)
        span = max(high - low, .01)
        dates = sorted({p.at for r in self.performance for p in r.daily_equity})
        indexes = {at: i + 1 for i, at in enumerate(dates)}
        curves = []
        for row in self.performance:
            points = [(0, 1.0)] + [(indexes[p.at], p.equity) for p in row.daily_equity]
            curves.append({"name": row.name, "points": " ".join(
                f"{50 + i / max(len(dates), 1) * 700:.2f},{20 + (high - v) / span * 220:.2f}"
                for i, v in points)})
        return {"low": low * 100, "high": high * 100, "curves": curves}


def _json(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 50_000_000:
        raise ValueError("report too large")
    result: dict[str, Any] = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(result, dict):
        raise ValueError("expected an object")
    return result


def _worker(root: Path, deployment: dict[str, Any]) -> dict[str, Any]:
    """A fresh completed receipt proves a check ran, never continuous trading."""
    if deployment.get('scheduled') is not True:
        return {}
    try:
        receipt = _json(root / 'worker.json')
        if (receipt['code_commit'] != deployment['learner_commit']
                or receipt['trading_enabled'] is not False or receipt['promotion'] != 'NONE'
                or deployment['interval_minutes'] != 10
                or receipt['status'] not in ('BLOCKED', 'ERROR')
                or type(receipt['resolved_this_run']) is not int
                or receipt['resolved_this_run'] < 0):
            raise ValueError('invalid worker receipt')
        labels = {
            'FORWARD_PRODUCER_MISSING': 'New-market forecast producer is not implemented.',
            'PAPER_PROMOTION_REQUIRED': (
                'This learner model is not approved for PAPER entry.'),
            'PAPER_EXECUTION_NOT_CONFIGURED': (
                'The learner PAPER execution adapter is not deployed.'),
            'OUTCOME_PROCESSING_FAILED': 'Outcome processing failed; inspect the worker.',
            'PROTECTED_FEATURE_WINDOW': (
                'The feature or target window overlaps protected research dates.'),
            'OUTSIDE_FORECAST_SESSION': (
                'Waiting for a market session with time to finish the 30-minute horizon.'),
            'INCOMPLETE_OR_STALE_MINUTE_DATA': (
                'Fresh, complete minute bars are not available for the full roster.'),
            'FORECAST_LOOKBACK_WARMUP': 'Waiting for 31 completed regular-session minutes.',
            'ALERT_STATE_FAILED': 'Alert persistence failed; inspect the alert store and policy.',
        }
        blockers = tuple(labels[key] for key in receipt['blockers'])
        timestamp = _timestamp(receipt['finished_at'])
        age = (datetime.now(UTC) - datetime.fromisoformat(timestamp)).total_seconds()
        if age < 0:
            raise ValueError('future worker receipt')
        status = 'STALE_WORKER' if age > 900 else (
            'WORKER_ERROR' if receipt['status'] == 'ERROR' else 'SCHEDULED_BLOCKED')
        reasons = {
            'STALE_WORKER': 'No completed worker check in the last 15 minutes.',
            'WORKER_ERROR': 'The latest outcome-processing check failed. Trading is disabled.',
            'SCHEDULED_BLOCKED': 'Scheduled every 10 minutes. The latest outcome check completed; '
                                 'new forecasts and learner trading remain blocked.',
        }
        intraday = None
        if 'intraday' in receipt:
            intraday = IntradayPanel(**{k: receipt['intraday'][k]
                                       for k in IntradayPanel.model_fields})
        progress = None
        if 'progress' in receipt:
            progress = ProgressPanel(
                **{k: receipt['progress'][k] for k in ProgressPanel.model_fields})
        return dict(status=status, reason=(
                    'The intraday worker completed its latest data and learning pass. '
                    'See the current forecast and PAPER entry blockers below.'
                    if intraday is not None and status == 'SCHEDULED_BLOCKED' else reasons[status]),
                    intraday=intraday, progress=progress, last_worker_at=timestamp,
                    interval_minutes=10, resolved_this_run=receipt['resolved_this_run'],
                    worker_blockers=blockers)
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _performance(root: Path, models: dict[str, dict[str, Any]]) -> tuple[
    tuple[PerformanceRow, ...], tuple[str, ...]
]:
    rows: list[PerformanceRow] = []
    errors = []
    baseline: dict[str, Any] | None = None

    def windows(report: dict[str, Any]) -> list[tuple[str, ...]]:
        return [tuple(p[k] for k in ("decision_at", "entry_at", "exit_at"))
                for p in report["periods"]]

    def row(report: dict[str, Any], name: str, key: str, model_id: str | None) -> PerformanceRow:
        return PerformanceRow(
            name=name, model_id=model_id, rank_ic=report["predictive"]["mean_ic"]
            if model_id else None, periods=report["period_count"],
            start=report["periods"][0]["entry_at"], end=report["periods"][-1]["exit_at"],
            **{k: report["portfolios"][key][k] for k in (
                "net_return", "max_drawdown", "excess_return_vs_spy", "cost_over_initial",
                "traded_notional_over_initial", "mean_daily_gross_exposure", "daily_equity")})

    for filename, name in (("mean", "Mean / equal-weight baseline"), ("ridge", "Ridge"),
                           ("ridge_successor", "Ridge successor")):
        try:
            payload = _json(root / "evidence" / (filename + "-economics.json"))
            report = payload["report"]
            if content_id(report) != payload["report_hash"]:
                raise ValueError("report integrity failure")
            lineage = report["lineage"]
            model = models[lineage["model_id"]]
            if model["family"] != ("mean" if filename == "mean" else "ridge"):
                raise ValueError("report family mismatch")
            if filename == "ridge_successor" and (
                model["version"] < 2 or not model.get("parent_model_id")
            ):
                raise ValueError("successor lineage missing")
            if filename == "mean" and (
                report["portfolios"]["top_quintile"] != report["portfolios"]["all_universe"]
            ):
                raise ValueError("constant mean report differs from equal-weight baseline")
            if (lineage["model_hash"] != content_id(model)
                    or lineage["dataset_id"] != model["dataset_id"]
                    or report["evidence_class"] != "EXPLORATORY_HISTORICAL_REPLAY"
                    or report["contract"] != "learner-next-open-fifth-close-liquidated-v1"
                    or report["promotion"] != "NONE"
                    or report["positive_expectancy_justified"] is not False
                    or str(report["costs"]["slippage_bps_per_side"]) != "5"
                    or str(report["costs"]["commission_per_order"]) != "0"):
                raise ValueError("report contract or model mismatch")
            if baseline is not None and (
                windows(report) != windows(baseline)
                or report["portfolios"]["spy"] != baseline["portfolios"]["spy"]
                or report["portfolios"]["all_universe"] != baseline["portfolios"]["all_universe"]
            ):
                raise ValueError("unmatched comparison windows or baselines")
            rows.append(row(report, name, "top_quintile", model["model_id"]))
            baseline = report
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            # Never render exception text or arbitrary file fields; those can contain secrets.
            errors.append(f"{name}: saved report unavailable or failed integrity/lineage checks.")
    if baseline is not None:
        if not any(r.name == "Mean / equal-weight baseline" for r in rows):
            rows.append(row(baseline, "Equal-weight baseline", "all_universe", None))
        rows.append(row(baseline, "Matched SPY", "spy", None))
    return tuple(rows), tuple(errors)


def read_learner_panel(directory: Path | None) -> LearnerPanel:
    if directory is None:
        return LearnerPanel()
    try:
        if not directory.is_absolute():
            raise ValueError("absolute configured directory required")
        path = directory / "state" / "learner.db"
        # mode=ro refuses missing paths and never creates/migrates a store. Use a transaction
        # so model metadata and counts describe one consistent SQLite snapshot.
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            models = {}
            for identity, digest, document in connection.execute(
                "SELECT model_id, document_hash, document_json FROM learner_model_versions"
            ):
                if hashlib.sha256(document.encode()).hexdigest() != digest:
                    raise ValueError("model document integrity failure")
                model = json.loads(document)
                if model["model_id"] != identity:
                    raise ValueError("model identity mismatch")
                models[identity] = model
            counts = dict(connection.execute(
                "SELECT evaluation_class, count(*) FROM learner_predictions "
                "GROUP BY evaluation_class"
            ))
            resolved = connection.execute("SELECT count(*) FROM learner_outcomes").fetchone()[0]
            latest_prediction = connection.execute(
                "SELECT max(recorded_at) FROM learner_predictions").fetchone()[0]
            latest_resolution = connection.execute(
                "SELECT max(resolved_at) FROM learner_outcomes").fetchone()[0]
            latest_evaluation = connection.execute(
                "SELECT max(created_at) FROM learner_evaluations").fetchone()[0]
            fits = connection.execute(
                "SELECT coalesce(sum(candidates_evaluated),0) FROM search_runs "
                "WHERE actor='elrond-learner'").fetchone()[0]
        finally:
            connection.close()
        try:
            deployment = _json(directory / "deployment.json")
        except (OSError, ValueError):
            deployment = {}
        status = "NOT_SCHEDULED" if deployment.get("scheduled") is False else "WORKER_UNVERIFIED"
        reason = ("Deployment records no scheduled learner. The historical run completed; "
                  "continuous new-market forecasting is not implemented."
                  if status == "NOT_SCHEDULED" else
                  "No verified learner worker heartbeat is available. Saved results do not prove "
                  "that a process is running.")
        worker = _worker(directory, deployment)
        performance, errors = _performance(directory, models)
        holdout = deployment.get("holdout", {})
        if not isinstance(holdout, dict):
            raise ValueError("invalid holdout declaration")
        return LearnerPanel(
            **(worker or dict(status=status, reason=reason)),
            historical_predictions=counts.get("HISTORICAL_REPLAY", 0),
            prospective_predictions=counts.get("PROSPECTIVE_LOCAL", 0),
            shadow_predictions=counts.get("SHADOW", 0), resolved_predictions=resolved,
            pending_predictions=sum(counts.values()) - resolved, fit_attempts=fits,
            last_prediction_at=latest_prediction, last_resolution_at=latest_resolution,
            last_evaluation_at=latest_evaluation, holdout_start=holdout.get("start"),
            holdout_end=holdout.get("end"), performance=performance, report_errors=errors,
            models=tuple(ModelRow(**{k: m[k] for k in ModelRow.model_fields})
                         for m in sorted(models.values(), key=lambda m: m["created_at"])))
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return LearnerPanel(status="UNAVAILABLE", reason=(
            "Learner store unavailable or incompatible. No counts or performance can be verified."))
