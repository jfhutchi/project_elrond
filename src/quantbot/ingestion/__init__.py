"""Durable ingestion control: cursors, run history, incidents and heartbeats (#198).

Missing data is the quietest way this project can lose future research capacity. A collector
that has been stopped for three weeks looks exactly like one that ran ten minutes ago, and by
the time a hypothesis needs that window the provider no longer serves it. This package makes
collection state durable and observable so that "nothing has been collected since the 14th" is
a question with an answer.

Everything here is operational state. It is not research evidence, it cannot appear in an
experiment manifest, and it has no authority over the broker, the risk engine or the kill
switch -- nor does the kill switch have authority over it. See `control.py` for why.
"""

from quantbot.ingestion.control import IngestionControlPlane
from quantbot.ingestion.models import (
    INCIDENT_KINDS,
    ComponentHeartbeat,
    CursorAdvance,
    CursorConflict,
    EventKind,
    HeartbeatStatus,
    IngestionCursor,
    IngestionError,
    IngestionEvent,
    IngestionRun,
    IngestionSource,
    RecordCounts,
    RunMode,
    RunStatus,
    Severity,
    SourceHealth,
    SourceStatus,
    resolve_health,
)
from quantbot.ingestion.report import DEFAULT_STALE_AFTER_HOURS, ingestion_status

__all__ = [
    "DEFAULT_STALE_AFTER_HOURS",
    "INCIDENT_KINDS",
    "ComponentHeartbeat",
    "CursorAdvance",
    "CursorConflict",
    "EventKind",
    "HeartbeatStatus",
    "IngestionControlPlane",
    "IngestionCursor",
    "IngestionError",
    "IngestionEvent",
    "IngestionRun",
    "IngestionSource",
    "RecordCounts",
    "RunMode",
    "RunStatus",
    "Severity",
    "SourceHealth",
    "SourceStatus",
    "ingestion_status",
    "resolve_health",
]
