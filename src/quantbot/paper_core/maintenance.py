"""Closed-session maintenance eligibility only; never grants entry/resume authority."""

from datetime import datetime


def closed_session_objections(
    *,
    now: datetime,
    started: datetime,
    completed: datetime,
    clock_at: datetime,
    is_open: bool,
    wall_anchor: datetime,
    next_open: datetime,
    provider_times: tuple[datetime, datetime],
    time_objections: tuple[str, ...],
    guardian_objections: tuple[str, ...],
) -> tuple[str, ...]:
    reasons = []
    times = (now, started, completed, clock_at, wall_anchor, next_open, *provider_times)
    if any(t.tzinfo is None for t in times):
        return ("MAINTENANCE_AWARE_TIMES_REQUIRED",)
    if is_open is not False or (next_open - now).total_seconds() <= 900:
        reasons.append("MAINTENANCE_CLOSED_WINDOW_REQUIRED")
    if not (
        0 <= (completed - started).total_seconds() <= 10
        and 0 <= (now - completed).total_seconds() <= 10
        and 0 <= (now - clock_at).total_seconds() <= 10
    ):
        reasons.append("MAINTENANCE_FRESH_BROKER_REQUIRED")
    if not (
        0 <= (now - wall_anchor).total_seconds() <= 30
        and provider_times[0] < provider_times[1]
        and all(0 <= (now - t).total_seconds() <= 30 for t in provider_times)
    ):
        reasons.append("MAINTENANCE_FRESH_INDEPENDENT_TIME_REQUIRED")
    if time_objections or set(guardian_objections) - {
        "SESSION_OPENING_UNVERIFIED",
    }:
        reasons.append("MAINTENANCE_OTHER_SAFETY_OBJECTION")
    return tuple(reasons)
