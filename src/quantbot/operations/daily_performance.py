"""Cash-flow-adjusted PAPER account performance, without invented historical closes.

The caller must provide reconciled, exchange-close evidence and verified external
flows. An account reset breaks the comparison epoch; it is never a trading gain.
Equity includes both realized and unrealized P&L and broker-charged costs. This
metric conveys no live-trading authority and does not promote a model.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

DayStatus = Literal["FINAL", "PENDING", "MISSING", "INVALID"]


@dataclass(frozen=True)
class CloseEvidence:
    session_date: date
    close_at: datetime
    captured_at: datetime
    previous_close_equity: Decimal | None
    close_equity: Decimal
    net_external_flow: Decimal
    flows_verified: bool
    same_account_epoch: bool
    reconciled: bool


@dataclass(frozen=True)
class DailyPerformance:
    session_date: date
    status: DayStatus
    net_pnl: Decimal | None
    reason: str | None = None


@dataclass(frozen=True)
class PerformanceSummary:
    net_pnl: Decimal
    valid_days: int
    profitable_days: int
    losing_days: int
    flat_days: int
    profitable_day_percent: Decimal | None
    first_session: date | None
    last_session: date | None


class CloseLedger:
    """Dedicated immutable close receipts; never migrate an operational database.

    Creation is explicit. A retry must carry identical evidence; conflicting close
    receipts require investigation and cannot silently rewrite a profitable day.
    """

    def __init__(self, path: Path, *, create: bool = False) -> None:
        path = path.resolve()
        self.path = path
        if not path.exists() and not create:
            raise FileNotFoundError("daily close ledger has not been created")
        with sqlite3.connect(path) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if tables and tables != {"paper_daily_closes_v1"}:
                raise ValueError("refusing an unrelated database")
            if not tables:
                if not create:
                    raise ValueError("daily close ledger is uninitialized")
                connection.execute(
                    "CREATE TABLE paper_daily_closes_v1 "
                    "(session_date TEXT PRIMARY KEY, evidence_json TEXT NOT NULL)"
                )

    def finalize(self, evidence: CloseEvidence) -> DailyPerformance:
        day = evaluate_day(evidence)
        if day.status != "FINAL":
            raise ValueError("only valid reconciled close evidence can be finalized")
        document = json.dumps(asdict(evidence), default=str, sort_keys=True)
        with sqlite3.connect(self.path, timeout=5) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT evidence_json FROM paper_daily_closes_v1 WHERE session_date=?",
                (evidence.session_date.isoformat(),),
            ).fetchone()
            if row is not None and row[0] != document:
                raise ValueError("conflicting immutable daily close evidence")
            if row is None:
                connection.execute("INSERT INTO paper_daily_closes_v1 VALUES (?, ?)",
                                   (evidence.session_date.isoformat(), document))
        return day

    def read_days(self) -> list[DailyPerformance]:
        with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT evidence_json FROM paper_daily_closes_v1 ORDER BY session_date"
            ).fetchall()
        days = []
        for (document,) in rows:
            payload = json.loads(document)
            payload["session_date"] = date.fromisoformat(payload["session_date"])
            for field in ("close_at", "captured_at"):
                payload[field] = datetime.fromisoformat(payload[field])
            for field in ("previous_close_equity", "close_equity", "net_external_flow"):
                payload[field] = Decimal(payload[field]) if payload[field] is not None else None
            days.append(evaluate_day(CloseEvidence(**payload)))
        return days


def evaluate_day(evidence: CloseEvidence) -> DailyPerformance:
    """Accept an actual reconciled close within five minutes of supplied close.

    The supplied close comes from the authoritative exchange calendar, including
    early closes. No wall-clock 16:00 assumption or historical interpolation.
    """
    def result(status: DayStatus, reason: str) -> DailyPerformance:
        return DailyPerformance(evidence.session_date, status, None, reason)

    for moment in (evidence.close_at, evidence.captured_at):
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("close evidence requires timezone-aware timestamps")
    exchange_date = evidence.close_at.astimezone(ZoneInfo("America/New_York")).date()
    if exchange_date != evidence.session_date:
        raise ValueError("exchange close does not belong to the supplied session date")
    if evidence.captured_at < evidence.close_at:
        return result("PENDING", "Exchange session has not closed.")
    if evidence.captured_at > evidence.close_at + timedelta(minutes=5):
        return result("INVALID", "No timely exchange-close snapshot.")
    if evidence.previous_close_equity is None:
        return result("MISSING", "Previous close baseline is unavailable.")
    values = (evidence.previous_close_equity, evidence.close_equity, evidence.net_external_flow)
    if not all(value.is_finite() for value in values):
        return result("INVALID", "Non-finite accounting evidence.")
    if not evidence.same_account_epoch:
        return result("INVALID", "Account reset or baseline epoch change.")
    if not evidence.flows_verified:
        return result("INVALID", "External cash flows have not been verified.")
    if not evidence.reconciled:
        return result("INVALID", "Close snapshot is not reconciled.")
    pnl = evidence.close_equity - evidence.previous_close_equity - evidence.net_external_flow
    return DailyPerformance(evidence.session_date, "FINAL", pnl)


def summarize(days: list[DailyPerformance]) -> PerformanceSummary:
    """All valid completed observed days are the denominator, including flats.

    Duplicate dates are excluded rather than counted twice or arbitrarily chosen.
    """
    counts: dict[date, int] = {}
    for day in days:
        counts[day.session_date] = counts.get(day.session_date, 0) + 1
    valid = [
        day for day in days
        if counts[day.session_date] == 1 and day.status == "FINAL"
        and day.net_pnl is not None and day.net_pnl.is_finite()
    ]
    pnls = [day.net_pnl for day in valid if day.net_pnl is not None]
    positive = sum(pnl > 0 for pnl in pnls)
    negative = sum(pnl < 0 for pnl in pnls)
    dates = [day.session_date for day in valid]
    return PerformanceSummary(
        net_pnl=sum(pnls, Decimal(0)), valid_days=len(valid),
        profitable_days=positive, losing_days=negative,
        flat_days=len(valid) - positive - negative,
        profitable_day_percent=Decimal(100) * positive / len(valid) if valid else None,
        first_session=min(dates) if dates else None,
        last_session=max(dates) if dates else None,
    )
