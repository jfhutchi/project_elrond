"""The operator-facing answer to "is anything still collecting?" (#198).

Kept out of `cli.py` so it can be tested without a process boundary, and out of `control.py` so
that the plane stays a plane: recording state and reporting on it are different jobs, and only
one of them has to be safe to call from anywhere.

The report is **read-only**. It opens no run, moves no cursor and records no incident. A status
command that wrote history would be the worst possible thing to run when trying to work out what
happened.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from quantbot.ingestion.control import IngestionControlPlane
from quantbot.ingestion.models import SourceHealth, utc

#: How long a source may go without a successful live run before it is called stale.
#:
#: **Project default, chosen from an existing deployed value.** `scripts/supervisor.py` has used
#: `STALE_RUN_HOURS = 100` since the daemon watchdog was written, for a reason that applies
#: unchanged here: consecutive daily cycles are 24h apart midweek, 72h across a weekend and 96h
#: when a Monday holiday intervenes, so a tighter bound cries wolf every long weekend and trains
#: an operator to ignore the warning -- which is worse than no warning at all.
#:
#: CLAUDE.md's tiebreak for an unspecified threshold takes an existing deployed value first, and
#: this is one. A caller that knows the market calendar should pass its own
#: `expected_success_after` to `IngestionControlPlane.status` instead: this default exists so a
#: bare `quantbot ingestion-status` is honest, not so nobody ever has to think about it.
DEFAULT_STALE_AFTER_HOURS = 100


def ingestion_status(
    plane: IngestionControlPlane,
    *,
    now: datetime,
    stale_after_hours: int = DEFAULT_STALE_AFTER_HOURS,
) -> dict[str, object]:
    """Every source, every stream, and when each last actually worked.

    A source with no streams is reported rather than skipped. "Registered and never collected"
    is precisely the state this whole issue exists to make visible, and a per-stream loop that
    found nothing to iterate would report it as silence.
    """
    moment = utc(now)
    expected_success_after = moment - timedelta(hours=stale_after_hours)

    sources: list[dict[str, object]] = []
    unhealthy = 0
    for source in plane.list_sources():
        cursors = plane.cursors_for(source.source_id)
        latest = plane.latest_run(source.source_id)
        incidents = plane.open_incidents(source.source_id)

        streams: list[dict[str, object]] = []
        # A registered source with no cursor still has a health answer, and it is the important
        # one. `""` is not a real stream key; it reaches `status` only so the health rules run.
        for stream_key in [cursor.stream_key for cursor in cursors] or [""]:
            status = plane.status(
                source.source_id,
                stream_key=stream_key,
                expected_success_after=expected_success_after,
            )
            if status.health is not SourceHealth.HEALTHY:
                unhealthy += 1
            streams.append(
                {
                    "stream_key": stream_key or None,
                    "health": status.health.value,
                    "last_success_at": (
                        None
                        if status.last_success_at is None
                        else status.last_success_at.isoformat()
                    ),
                    "last_observation_at": (
                        None
                        if status.last_observation_at is None
                        else status.last_observation_at.isoformat()
                    ),
                    "hours_since_success": (
                        None
                        if status.last_success_at is None
                        else round(
                            (moment - status.last_success_at).total_seconds() / 3600.0, 2
                        )
                    ),
                    "reason": status.reason,
                }
            )

        sources.append(
            {
                "source_id": source.source_id,
                "provider": source.provider,
                "source_kind": source.source_kind,
                "enabled": source.enabled,
                "configuration_hash": source.configuration_hash,
                "streams": streams,
                "latest_run": (
                    None
                    if latest is None
                    else {
                        "ingestion_run_id": latest.ingestion_run_id,
                        "mode": latest.mode.value,
                        "status": latest.status.value,
                        "started_at": latest.started_at.isoformat(),
                        "finished_at": (
                            None if latest.finished_at is None else latest.finished_at.isoformat()
                        ),
                        "records_seen": latest.counts.seen,
                        "records_inserted": latest.counts.inserted,
                        "records_duplicate": latest.counts.duplicate,
                        "records_rejected": latest.counts.rejected,
                        "records_unaccounted": latest.counts.unaccounted,
                        "error_code": latest.error_code,
                    }
                ),
                "open_incidents": [
                    {
                        "event_kind": incident.event_kind.value,
                        "severity": incident.severity.value,
                        "occurred_at": incident.occurred_at.isoformat(),
                        "stream_key": incident.stream_key,
                        "detail": incident.detail,
                    }
                    for incident in incidents
                ],
            }
        )

    return {
        "ok": True,
        "as_of": moment.isoformat(),
        "stale_after_hours": stale_after_hours,
        "expected_success_after": expected_success_after.isoformat(),
        "sources": sources,
        "unhealthy_streams": unhealthy,
        # Stated rather than left to inference. A reader who sees no sources should conclude
        # that collection is unconfigured, not that it is fine.
        "note": (
            "no source registered means nothing is collecting, not that everything is healthy; "
            "ingestion is deliberately independent of the trading kill switch"
        ),
    }


__all__ = ["DEFAULT_STALE_AFTER_HOURS", "ingestion_status"]
