"""Read-only forward economics; no simulated return can become broker profit.

The existing reconciled daily-close reader remains the account accounting source.
Callers supply its as-of rows and an authoritative calendar covering the complete
prospective period. Missing days/calendar/lot attribution remain unknown.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from quantbot.learner.experiment_contract import DecisionRecord, Digest, ExperimentManifest
from quantbot.learner.experiment_coordinator import ResolvedMarketLabel, feature_key
from quantbot.learner.experiment_lots import LotEvent, resolve_lot
from quantbot.learner.experiment_observation import ObservationBatch
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.forward_source import Snapshot
from quantbot.learner.models import Finite, LearnerModel, Text
from quantbot.operations.daily_performance import DailyPerformance, summarize
from quantbot.research.manifest import content_id
from quantbot.strategy import XNYSSession, XNYSSessionSequence

EXCHANGE_ZONE = ZoneInfo("America/New_York")


class EvaluationCalendarEvidence(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    coverage_start: date
    coverage_end: date
    sessions: XNYSSessionSequence
    source_hash: Digest
    synthetic: bool


def calendar_evidence(manifest: ExperimentManifest, source: Snapshot) -> EvaluationCalendarEvidence:
    """Calendar request bounds from retained collect_snapshot's exact contract."""
    from datetime import timedelta

    if not any(
        receipt.url == "https://paper-api.alpaca.markets/v2/calendar" and receipt.status_code == 200
        for receipt in source.receipts
    ):
        raise ValueError("AUTHORITATIVE_CALENDAR_RECEIPT_REQUIRED")
    return EvaluationCalendarEvidence(
        experiment_id=manifest.experiment_id,
        recorded_at=source.received_at,
        coverage_start=source.query.start.date(),
        coverage_end=source.query.end.date() + timedelta(days=14),
        sessions=source.sessions,
        source_hash=source.identity,
        synthetic=manifest.synthetic,
    )


def _stored_calendar(
    store: ExperimentStore, manifest: ExperimentManifest, now: datetime
) -> tuple[XNYSSessionSequence | None, tuple[date, date] | None]:
    from datetime import timedelta

    rows = [
        event.decode(EvaluationCalendarEvidence)
        for event in store.read_as_of(manifest.experiment_id, now)
        if event.kind == "EvaluationCalendarEvidence"
    ]
    rows = sorted(
        (row for row in rows if row.synthetic == manifest.synthetic),
        key=lambda row: (row.coverage_start, row.coverage_end),
    )
    start = manifest.prospective_start_at.astimezone(EXCHANGE_ZONE).date()
    cursor = start
    known: dict[date, XNYSSession] = {}
    for row in rows:
        if row.coverage_end < start:
            continue
        if row.coverage_start > cursor:
            break
        if row.sessions.calendar != "XNYS" or row.coverage_end < row.coverage_start:
            raise ValueError("EVALUATION_CALENDAR_BOUNDS_UNVERIFIED")
        for session in row.sessions.sessions:
            if not row.coverage_start <= session.session_date <= row.coverage_end:
                raise ValueError("EVALUATION_CALENDAR_BOUNDS_UNVERIFIED")
            previous = known.get(session.session_date)
            if previous is not None and previous != session:
                raise ValueError("EVALUATION_CALENDAR_CONFLICT_REQUIRES_REVIEW")
            known[session.session_date] = session
        cursor = max(cursor, row.coverage_end + timedelta(days=1))
    if cursor <= now.astimezone(EXCHANGE_ZONE).date() or len(known) < 2:
        return None, None
    sequence = XNYSSessionSequence(
        calendar="XNYS", sessions=tuple(known[day] for day in sorted(known))
    )
    return sequence, (start, cursor - timedelta(days=1))


class ForecastAssessment(LearnerModel):
    model_id: Digest
    labeled_rows: int
    independent_sessions: int
    nonoverlapping_time_blocks: int
    mse: Finite
    mae: Finite
    matched_comparison_rows: int
    metric: Literal["RAW_RETURN_FORECAST_ERROR_NOT_BROKER_PNL"] = (
        "RAW_RETURN_FORECAST_ERROR_NOT_BROKER_PNL"
    )


class EvaluationReport(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    manifest_hash: Digest
    status: Literal[
        "ACCUMULATING",
        "ASSESSMENT_AVAILABLE",
        "INSUFFICIENT_EVIDENCE",
        "CALENDAR_COVERAGE_REQUIRED",
    ]
    scheduled_sessions: int | None
    coverage_valid_research_sessions: int
    eligible_entry_decisions: int
    authorized_cost_cleared_forecasts: int
    zero_eligibility_review_required: bool
    new_research_allowed: bool
    new_entries_allowed: bool
    position_protection_required: Literal[True] = True
    assessment_ready: bool
    forecasts: tuple[ForecastAssessment, ...]
    completed_owned_lots: int
    incomplete_owned_lots: int
    learner_realized_pnl: Decimal | None
    learner_fees: Decimal | None
    account_net_pnl: Decimal | None
    account_drawdown_usd: Decimal | None
    valid_performance_days: int
    profitable_days: int
    losing_days: int
    flat_days: int
    profitable_day_percent: Decimal | None
    first_performance_session: date | None
    last_performance_session: date | None
    attribution: Literal["LEGACY_UNREALIZED_AND_UNATTRIBUTED_NOT_SEPARATELY_VERIFIED"] = (
        "LEGACY_UNREALIZED_AND_UNATTRIBUTED_NOT_SEPARATELY_VERIFIED"
    )
    reference_simulated_pnl: None = None
    requires_model_approval: Literal[False] = False
    live_authority: Literal[False] = False
    next_owner: Text
    synthetic: bool


def evaluate_experiment(
    store: ExperimentStore,
    manifest: ExperimentManifest,
    *,
    now: datetime,
    sessions: XNYSSessionSequence | None = None,
    calendar_coverage: tuple[date, date] | None = None,
    account_days: Sequence[DailyPerformance] = (),
) -> EvaluationReport:
    manifest = ExperimentManifest.model_validate(manifest.model_dump())
    if now.tzinfo is None or now.utcoffset() is None or now < manifest.registered_at:
        raise ValueError("ACTUAL_AS_OF_EVALUATION_TIME_REQUIRED")
    if store.state(manifest.experiment_id).manifest_hash != content_id(manifest):
        raise ValueError("FROZEN_MANIFEST_MISMATCH")
    start_date = manifest.prospective_start_at.astimezone(EXCHANGE_ZONE).date()
    today = now.astimezone(EXCHANGE_ZONE).date()
    if sessions is None and calendar_coverage is None:
        sessions, calendar_coverage = _stored_calendar(store, manifest, now)
    calendar_known = (
        sessions is not None
        and sessions.calendar == "XNYS"
        and calendar_coverage is not None
        and calendar_coverage[0] <= start_date <= today <= calendar_coverage[1]
    )
    completed = {
        row.session_date
        for row in (sessions.sessions if sessions is not None and calendar_known else ())
        if manifest.prospective_start_at <= row.open_at and row.close_at <= now
    }
    scheduled = len(completed) if calendar_known else None
    events = store.read_as_of(manifest.experiment_id, now)
    observations = [e.decode(ObservationBatch) for e in events if e.kind == "ObservationBatch"]
    coverage_dates = {
        row.feature_at.astimezone(EXCHANGE_ZONE).date()
        for batch in observations
        if batch.synthetic == manifest.synthetic
        and len(batch.coverage) == len(manifest.roster)
        and all(row.status == "AVAILABLE" for row in batch.coverage)
        for row in batch.features
        if row.feature_at >= manifest.prospective_start_at
    }
    decisions = [e.decode(DecisionRecord) for e in events if e.kind == "DecisionRecord"]
    decisions = [
        row
        for row in decisions
        if row.synthetic == manifest.synthetic
        and row.features.evaluation_class == "PROSPECTIVE_LOCAL"
        and row.features.feature_at >= manifest.prospective_start_at
    ]
    labels = [e.decode(ResolvedMarketLabel) for e in events if e.kind == "ResolvedMarketLabel"]
    label_counts = Counter(feature_key(row.label.features) for row in labels)
    labeled = {
        feature_key(row.label.features): row.label
        for row in labels
        if row.synthetic == manifest.synthetic
        and row.label.evaluation_class == "PROSPECTIVE_LOCAL"
        and row.label.available_at <= now
        and label_counts[feature_key(row.label.features)] == 1
    }
    counts = Counter((row.model_id, feature_key(row.features)) for row in decisions)
    matched: dict[str, dict[str, tuple[DecisionRecord, float]]] = {}
    for row in decisions:
        key = feature_key(row.features)
        label = labeled.get(key)
        if label is not None and label.features == row.features and counts[row.model_id, key] == 1:
            matched.setdefault(row.model_id, {})[key] = (row, row.score - label.raw_return)
    common = (
        set.intersection(*(set(rows) for rows in matched.values())) if len(matched) > 1 else set()
    )
    forecasts = []
    for model_id, rows in sorted(matched.items()):
        blocks = sorted({(r.features.feature_at, r.features.target_at) for r, _ in rows.values()})
        last_target = None
        nonoverlapping = 0
        for feature_at, target_at in blocks:
            if last_target is None or feature_at >= last_target:
                nonoverlapping += 1
                last_target = target_at
        errors = [error for _, error in rows.values()]
        forecasts.append(
            ForecastAssessment(
                model_id=model_id,
                labeled_rows=len(errors),
                independent_sessions=len(
                    {
                        r.features.feature_at.astimezone(EXCHANGE_ZONE).date()
                        for r, _ in rows.values()
                    }
                ),
                nonoverlapping_time_blocks=nonoverlapping,
                mse=sum(error * error for error in errors) / len(errors),
                mae=sum(abs(error) for error in errors) / len(errors),
                matched_comparison_rows=len(common),
            )
        )
    declared = {
        row.lot_id
        for event in events
        if event.kind == "LotEvent"
        for row in (event.decode(LotEvent),)
        if row.kind == "ENTRY_DECLARED" and row.synthetic == manifest.synthetic
    }
    outcomes = [resolve_lot(store, lot_id, now=now) for lot_id in sorted(declared)]
    complete = [row for row in outcomes if row.status == "COMPLETE"]
    real = [row for row in complete if not row.synthetic]
    summary_days = [day for day in account_days if day.session_date in completed]
    summary = summarize(summary_days)
    day_counts = Counter(day.session_date for day in summary_days)
    valid = sorted(
        (
            day
            for day in summary_days
            if day_counts[day.session_date] == 1
            and day.status == "FINAL"
            and day.net_pnl is not None
            and day.net_pnl.is_finite()
        ),
        key=lambda day: day.session_date,
    )
    # A gap prevents a verified period drawdown; no zero-valued missing sessions.
    drawdown = None
    if valid and {day.session_date for day in valid} == completed:
        equity, peak, drawdown = Decimal(0), Decimal(0), Decimal(0)
        for day in valid:
            assert day.net_pnl is not None
            equity += day.net_pnl
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
    ready = summary.valid_days >= manifest.performance_sessions
    exhausted = scheduled is not None and scheduled >= manifest.maximum_evaluation_sessions
    review = (
        len(coverage_dates & completed) >= manifest.zero_eligibility_review_sessions
        and not declared
    )
    status = (
        "CALENDAR_COVERAGE_REQUIRED"
        if not calendar_known
        else "ASSESSMENT_AVAILABLE"
        if ready
        else "INSUFFICIENT_EVIDENCE"
        if exhausted
        else "ACCUMULATING"
    )
    return EvaluationReport(
        experiment_id=manifest.experiment_id,
        recorded_at=now,
        manifest_hash=content_id(manifest),
        status=status,
        scheduled_sessions=scheduled,
        coverage_valid_research_sessions=len(coverage_dates & completed),
        eligible_entry_decisions=len(declared),
        authorized_cost_cleared_forecasts=sum(row.paper_eligible for row in decisions),
        zero_eligibility_review_required=review,
        new_research_allowed=calendar_known and not exhausted,
        new_entries_allowed=calendar_known and not exhausted,
        assessment_ready=ready,
        forecasts=tuple(forecasts),
        completed_owned_lots=len(complete),
        incomplete_owned_lots=len(outcomes) - len(complete),
        learner_realized_pnl=sum(
            (row.net_pnl for row in real if row.net_pnl is not None), Decimal(0)
        )
        if real
        else None,
        learner_fees=sum((row.fees for row in real if row.fees is not None), Decimal(0))
        if real
        else None,
        account_net_pnl=summary.net_pnl if summary.valid_days else None,
        account_drawdown_usd=drawdown,
        valid_performance_days=summary.valid_days,
        profitable_days=summary.profitable_days,
        losing_days=summary.losing_days,
        flat_days=summary.flat_days,
        profitable_day_percent=summary.profitable_day_percent,
        first_performance_session=summary.first_session,
        last_performance_session=summary.last_session,
        next_owner="DEVELOPMENT_OPERATOR"
        if review or exhausted or not calendar_known
        else "SCHEDULED_COORDINATOR",
        synthetic=manifest.synthetic,
    )
