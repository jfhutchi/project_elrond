"""Read-only presentation of supplied daily P&L; never infer observations from time."""

from __future__ import annotations

import calendar
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol


class PerformanceRow(Protocol):
    @property
    def session_date(self) -> date: ...
    @property
    def status(self) -> str: ...
    @property
    def net_pnl(self) -> Decimal | None: ...
    @property
    def reason(self) -> str | None: ...


@dataclass(frozen=True)
class PerformanceData:
    rows: Sequence[PerformanceRow] = ()
    session_dates: frozenset[date] | None = None
    calendar_start: date | None = None
    calendar_end: date | None = None
    available: bool = True
    reason: str | None = None


@dataclass(frozen=True)
class PerformanceSummary:
    total: str | None
    tone: str
    profitable: int
    losing: int
    flat: int
    sample_size: int
    profitable_percent: str | None
    period: str | None


@dataclass(frozen=True)
class CalendarCell:
    day: int | None
    session_date: str | None = None
    status: str = ""
    amount: str | None = None
    tone: str = "neutral"
    reason: str | None = None
    paper_costs: dict[str, Any] | None = None


@dataclass(frozen=True)
class CalendarPanel:
    month: str
    title: str
    previous_month: str | None
    next_month: str | None
    weeks: tuple[tuple[CalendarCell, ...], ...]
    summary: PerformanceSummary
    available: bool
    reason: str | None
    calendar_known: bool


def _money(value: Decimal) -> str:
    sign = "+" if value > 0 else "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _tone(value: Decimal) -> str:
    return "positive" if value > 0 else "negative" if value < 0 else "neutral"


def build_calendar(year: int, month: int, data: PerformanceData) -> CalendarPanel:
    """A Monday-first grid and all-observed-period totals from the same validated rows.

    Calendar coverage is explicit: dates outside it remain unknown, even on weekends.
    Duplicate dates are invalid rather than arbitrarily selecting one of their values.
    """
    date(year, month, 1)  # validate even if the reader supplied no observations
    counts = Counter(row.session_date for row in data.rows)
    rows = {row.session_date: row for row in data.rows}
    valid = {
        day: row.net_pnl
        for day, row in rows.items()
        if data.available
        and counts[day] == 1
        and row.status == "FINAL"
        and row.net_pnl is not None
        and row.net_pnl.is_finite()
    }
    amounts = list(valid.values())
    total = sum(amounts, Decimal("0"))
    sample = len(amounts)
    profitable = sum(amount > 0 for amount in amounts)
    summary = PerformanceSummary(
        _money(total) if sample else None,
        _tone(total),
        profitable,
        sum(amount < 0 for amount in amounts),
        sum(amount == 0 for amount in amounts),
        sample,
        f"{Decimal(profitable) / sample * 100:.1f}%" if sample else None,
        f"{min(valid).isoformat()} to {max(valid).isoformat()}" if sample else None,
    )
    known = (
        data.session_dates is not None
        and data.calendar_start is not None
        and data.calendar_end is not None
    )

    def cell(day: int) -> CalendarCell:
        if not day:
            return CalendarCell(None)
        current = date(year, month, day)
        if not data.available:
            return CalendarCell(day, current.isoformat(), "UNAVAILABLE", reason=data.reason)
        row = rows.get(current)
        if row is not None:
            if current in valid:
                value = valid[current]
                return CalendarCell(day, current.isoformat(), "FINAL", _money(value), _tone(value))
            invalid = counts[current] > 1 or row.status not in {"PENDING", "MISSING", "INVALID"}
            return CalendarCell(
                day,
                current.isoformat(),
                "INVALID" if invalid else row.status,
                reason="Duplicate daily rows" if counts[current] > 1 else row.reason,
            )
        if (
            known
            and data.calendar_start is not None
            and data.calendar_end is not None
            and data.calendar_start <= current <= data.calendar_end
            and current not in (data.session_dates or ())
        ):
            status = "WEEKEND" if current.weekday() >= 5 else "HOLIDAY"
            return CalendarCell(day, current.isoformat(), status)
        return CalendarCell(day, current.isoformat(), "MISSING", reason="No daily observation")

    previous = (
        date(year - 1, 12, 1)
        if month == 1 and year > 1
        else (date(year, month - 1, 1) if month > 1 else None)
    )
    following = (
        date(year + 1, 1, 1)
        if month == 12 and year < 9999
        else (date(year, month + 1, 1) if month < 12 else None)
    )
    return CalendarPanel(
        f"{year:04d}-{month:02d}",
        f"{calendar.month_name[month]} {year}",
        previous.strftime("%Y-%m") if previous else None,
        following.strftime("%Y-%m") if following else None,
        tuple(
            tuple(cell(day) for day in week)
            for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month)
        ),
        summary,
        data.available,
        data.reason,
        known,
    )
