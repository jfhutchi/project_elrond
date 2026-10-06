"""Strict, read-only local evidence adapter for the operator P&L calendar."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import fields
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, TypeAdapter

from quantbot.console.calendar import PerformanceData
from quantbot.operations.daily_performance import CloseEvidence, DailyPerformance, evaluate_day

REASONS = {
    "BASELINE_MISSING": "Previous close baseline unavailable.",
    "FLOW_UNVERIFIED": "External cash flows unverified.",
    "ACCOUNT_EPOCH_CHANGED": "Account epoch changed.",
    "RECONCILIATION_UNVERIFIED": "Close reconciliation unverified.",
    "CLOSE_NOT_CAPTURED": "Timely exchange close not captured.",
    "SESSION_PENDING": "Exchange session pending.",
    "CALENDAR_UNAVAILABLE": "Exchange calendar unavailable.",
    "CAPTURE_UNAVAILABLE": "Close capture unavailable.",
}


class Session(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_date: date
    open_at: AwareDatetime
    close_at: AwareDatetime


class CalendarCache(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    calendar: Literal["XNYS"]
    start: date
    end: date
    sessions: list[Session]


class CaptureStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_date: date
    status: Literal["MISSING", "INVALID", "PENDING"]
    reason: str


def exchange_today(now: datetime | None = None) -> date:
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Aware clock required")
    return moment.astimezone(ZoneInfo("America/New_York")).date()


def _reject_duplicate_keys(document: str | bytes) -> None:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    json.loads(document, object_pairs_hook=unique)


def _document(path: Path) -> bytes:
    document = path.read_bytes()
    _reject_duplicate_keys(document)
    return document


def _unavailable(reason: str) -> PerformanceData:
    return PerformanceData(available=False, reason=reason)


def read_configured_performance(
    environ: Mapping[str, str] | None = None, *, now: datetime | None = None
) -> PerformanceData:
    config = os.environ if environ is None else environ
    value = config.get("QUANTBOT_DAILY_PERFORMANCE_DIRECTORY")
    if not value:
        learner = config.get("QUANTBOT_LEARNER_DIRECTORY")
        value = str(Path(learner) / "performance") if learner else None
    if not value:
        return _unavailable("Daily performance directory unconfigured.")
    directory = Path(value)
    if not directory.is_absolute():
        return _unavailable("Daily performance directory must be absolute.")
    return read_performance(directory, now=now)


def read_performance(directory: Path, *, now: datetime | None = None) -> PerformanceData:
    """Read existing files only; errors never reveal paths, identifiers or raw JSON.

    Calendar coverage must include exchange today and every finalized close. Status is
    an optional current-day diagnostic; stale status cannot overwrite final history.
    """
    moment = now or datetime.now(UTC)
    today = exchange_today(moment)
    try:
        calendar = CalendarCache.model_validate_json(_document(directory / "calendar.json"))
        if not calendar.start <= today <= calendar.end:
            return _unavailable("Exchange calendar coverage stale or unavailable.")
        sessions: dict[date, Session] = {}
        for session in calendar.sessions:
            if (
                session.session_date in sessions
                or not calendar.start <= session.session_date <= calendar.end
                or session.open_at >= session.close_at
                or exchange_today(session.open_at) != session.session_date
                or exchange_today(session.close_at) != session.session_date
            ):
                raise ValueError("Invalid session")
            sessions[session.session_date] = session
        if not sessions:
            raise ValueError("Empty calendar")
        ordered_dates = sorted(sessions)
        preceding = dict(zip(ordered_dates[1:], ordered_dates[:-1], strict=True))
        path = (directory / "closes.db").resolve()
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables != {"paper_daily_closes_v1"}:
                raise ValueError("Invalid ledger schema")
            documents = connection.execute(
                "SELECT session_date, evidence_json FROM paper_daily_closes_v1 "
                "ORDER BY session_date"
            ).fetchall()
        rows: list[DailyPerformance] = []
        seen: set[date] = set()
        expected_fields = {field.name for field in fields(CloseEvidence)}
        for stored_date, document in documents:
            _reject_duplicate_keys(document)
            if set(json.loads(document)) != expected_fields:
                raise ValueError("Invalid close schema")
            evidence = TypeAdapter(CloseEvidence).validate_json(document, strict=True)
            matched_session = sessions.get(evidence.session_date)
            previous_date = preceding.get(evidence.session_date)
            if (
                stored_date != evidence.session_date.isoformat()
                or evidence.session_date in seen
                or matched_session is None
                or evidence.close_at != matched_session.close_at
                or previous_date is None
                or evidence.previous_session_date != previous_date
                or evidence.expected_previous_session_date != previous_date
                or evidence.captured_at > moment
            ):
                raise ValueError("Unmatched close evidence")
            day = evaluate_day(evidence)
            if day.status != "FINAL":
                raise ValueError("Non-final persisted close")
            seen.add(day.session_date)
            rows.append(day)
        status_path = directory / "capture-status.json"
        status = (
            CaptureStatus.model_validate_json(_document(status_path))
            if status_path.exists()
            else None
        )
        if status is not None and status.reason not in REASONS:
            raise ValueError("Unknown status reason")
        if today in sessions and today not in seen:
            if status is not None and status.session_date == today:
                rows.append(DailyPerformance(today, status.status, None, REASONS[status.reason]))
            else:
                pending = moment < sessions[today].close_at
                rows.append(
                    DailyPerformance(
                        today,
                        "PENDING" if pending else "MISSING",
                        None,
                        REASONS["SESSION_PENDING" if pending else "CLOSE_NOT_CAPTURED"],
                    )
                )
        return PerformanceData(
            rows=rows,
            session_dates=frozenset(sessions),
            calendar_start=calendar.start,
            calendar_end=calendar.end,
        )
    except (OSError, ValueError, TypeError, sqlite3.Error, ArithmeticError):
        return _unavailable("Daily performance evidence unavailable or invalid.")
