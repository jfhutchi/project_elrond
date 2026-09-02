"""Immutable descriptions of ingestion control state (#198).

Operational state, not research evidence. Nothing here may appear in an experiment manifest, and
nothing here carries authority over the broker, the risk engine or the kill switch.

The distinctions these types exist to keep sharp are the ones an operator cannot recover once
they are blurred:

* **Disabled is not unhealthy.** "Nobody turned it on", "somebody turned it off" and "it stopped
  and nobody noticed" are three different facts, and only the middle one is benign. A single
  boolean `healthy` collapses them.
* **Backfill is not collection.** A source caught up historically last night is not a source
  collecting today. `RunMode` keeps them apart in the row, and `IngestionCursor.last_success_at`
  -- which freshness reads -- is writable only by a `LIVE` run.
* **Silence is not health.** A collector that stopped emitting must not be able to close its own
  incident by going quiet, so an incident is resolved by an observation of recovery rather than
  by the absence of further complaints.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def normalize_datetime(cls, value: object) -> object:
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)


class RunMode(StrEnum):
    """Whether a run is keeping up or catching up.

    `BACKFILL` exists so historical catch-up cannot masquerade as forward evidence merely
    because it was ingested later. It is a property of the run, recorded at the point the run
    starts, rather than something inferred afterwards from the range it asked for.
    """

    LIVE = "LIVE"
    BACKFILL = "BACKFILL"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    #: Started, never finished, and observed later by something that outlived it. A process
    #: killed mid-batch leaves a `RUNNING` row that nothing will ever close from the inside.
    ABANDONED = "ABANDONED"


class EventKind(StrEnum):
    SOURCE_REGISTERED = "SOURCE_REGISTERED"
    CONFIGURATION_CHANGED = "CONFIGURATION_CHANGED"
    OPERATOR_PAUSED = "OPERATOR_PAUSED"
    OPERATOR_RESUMED = "OPERATOR_RESUMED"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    STALE_DETECTED = "STALE_DETECTED"
    RUN_ABANDONED = "RUN_ABANDONED"
    CURSOR_RESET = "CURSOR_RESET"
    RECOVERED = "RECOVERED"


#: Event kinds that open an incident: they stay unresolved until something observes recovery.
#: Everything else is a point-in-time note about what happened.
INCIDENT_KINDS: frozenset[EventKind] = frozenset(
    {EventKind.PROVIDER_FAILURE, EventKind.STALE_DETECTED, EventKind.RUN_ABANDONED}
)


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class SourceHealth(StrEnum):
    """What an operator actually needs to distinguish.

    `NEVER_COLLECTED` is separate from `STALE` on purpose. A source that has never succeeded has
    no gap to measure and no position to resume from; a source that succeeded and then stopped
    has both, and the recovery for the two is not the same.
    """

    DISABLED = "DISABLED"
    NEVER_COLLECTED = "NEVER_COLLECTED"
    HEALTHY = "HEALTHY"
    STALE = "STALE"
    FAILED = "FAILED"


class HeartbeatStatus(StrEnum):
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"


class IngestionSource(FrozenModel):
    """One configured logical collector."""

    source_id: Text
    provider: Text
    source_kind: Text
    configuration_hash: Digest
    enabled: bool
    created_at: datetime
    updated_at: datetime


class IngestionCursor(FrozenModel):
    """The durable position of one exact stream, and when it last actually worked."""

    source_id: Text
    stream_key: Text
    cursor_value: str | None = None
    #: Newest observation this stream has seen, from any run mode.
    last_observation_at: datetime | None = None
    #: When live collection last succeeded. Never written by a backfill.
    last_success_at: datetime | None = None
    updated_at: datetime
    version: int = Field(ge=1)


class CursorAdvance(FrozenModel):
    """A requested checkpoint move, carrying the version it believes it is replacing.

    There is deliberately no `last_success_at` field. The control plane derives it from the
    run's mode, so a backfill cannot set it however it is called -- the freshness marker is not
    something a caller gets to pass in.
    """

    stream_key: Text
    cursor_value: str | None = None
    last_observation_at: datetime | None = None
    #: `None` for the first checkpoint on a stream, otherwise the version being replaced. A
    #: mismatch is refused rather than merged: two collectors racing on one stream is exactly
    #: where a cursor silently goes backwards and a window is skipped forever.
    expected_version: int | None = Field(default=None, ge=1)


class RecordCounts(FrozenModel):
    """What a run saw and what became of it.

    `seen` is the denominator and the other three partition it. The residue -- seen minus the
    three -- is what the run looked at and never accounted for, which is the interesting number
    when a batch dies halfway.
    """

    seen: int = Field(default=0, ge=0)
    inserted: int = Field(default=0, ge=0)
    duplicate: int = Field(default=0, ge=0)
    rejected: int = Field(default=0, ge=0)

    def model_post_init(self, _context: object) -> None:
        if self.inserted + self.duplicate + self.rejected > self.seen:
            raise ValueError("record outcomes cannot exceed what the run saw")

    @property
    def unaccounted(self) -> int:
        return self.seen - self.inserted - self.duplicate - self.rejected


class IngestionRun(FrozenModel):
    """One append-only collection attempt."""

    ingestion_run_id: Text
    source_id: Text
    mode: RunMode
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    requested_start: str | None = None
    requested_end: str | None = None
    counts: RecordCounts = RecordCounts()
    error_code: str | None = None
    detail: str = ""

    def model_post_init(self, _context: object) -> None:
        running = self.status is RunStatus.RUNNING
        if running != (self.finished_at is None):
            raise ValueError("a finished run has an end and a running one does not")
        if self.error_code is not None and self.status not in {
            RunStatus.FAILED,
            RunStatus.ABANDONED,
        }:
            raise ValueError("only an unsuccessful run carries an error code")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("a run cannot finish before it started")


class IngestionEvent(FrozenModel):
    """One durable operational fact. Incidents stay open until recovery is observed."""

    event_id: Text
    source_id: Text
    stream_key: str | None = None
    event_kind: EventKind
    severity: Severity
    occurred_at: datetime
    resolved_at: datetime | None = None
    detail: str = ""

    def model_post_init(self, _context: object) -> None:
        if self.resolved_at is not None and self.resolved_at < self.occurred_at:
            raise ValueError("an event cannot resolve before it occurred")

    @property
    def open(self) -> bool:
        return self.resolved_at is None


class ComponentHeartbeat(FrozenModel):
    """One health sample from one process."""

    heartbeat_id: Text
    component_id: Text
    instance_id: Text
    observed_at: datetime
    status: HeartbeatStatus
    last_success_at: datetime | None = None
    detail: str = ""


class SourceStatus(FrozenModel):
    """The answer to "is this source collecting, and when did it last work?"."""

    source_id: Text
    health: SourceHealth
    enabled: bool
    last_success_at: datetime | None = None
    last_observation_at: datetime | None = None
    #: What the caller said should have been collected by now. `None` means nothing was due --
    #: the market is closed, or no new session has completed -- which is why a closed market
    #: cannot be reported as a feed outage.
    expected_success_after: datetime | None = None
    latest_run_id: str | None = None
    latest_run_status: RunStatus | None = None
    open_incidents: int = Field(default=0, ge=0)
    reason: str = ""

    @property
    def collecting(self) -> bool:
        """True only for a source that is enabled and currently keeping up.

        Deliberately not the inverse of any single failure: `NEVER_COLLECTED`, `STALE` and
        `FAILED` are all "not collecting" and all mean different things, which is why the
        health enum is what gets reported and this is only a convenience.
        """
        return self.health is SourceHealth.HEALTHY


class IngestionError(RuntimeError):
    """Raised when a control-plane transition would misdescribe what actually happened."""


class CursorConflict(IngestionError):
    """Raised when a checkpoint move is based on a version that is no longer current."""


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def resolve_health(
    *,
    source: IngestionSource,
    cursor: IngestionCursor | None,
    latest_run: IngestionRun | None,
    expected_success_after: datetime | None,
) -> tuple[SourceHealth, str]:
    """Classify one source. Pure, so the ordering of these rules is testable on its own.

    `expected_success_after` is supplied by the caller rather than computed here, because
    deciding when a collection was due is a market-calendar question and this module has no
    business owning one. Passing `None` says nothing was due -- which is how a legitimately
    closed market is prevented from looking like a feed outage.
    """
    if not source.enabled:
        return SourceHealth.DISABLED, "the source is disabled; it is not expected to collect"

    last_success = None if cursor is None else cursor.last_success_at

    if latest_run is not None and latest_run.status in {RunStatus.FAILED, RunStatus.ABANDONED}:
        if last_success is None or last_success < latest_run.started_at:
            verb = "failed" if latest_run.status is RunStatus.FAILED else "was abandoned"
            return (
                SourceHealth.FAILED,
                f"the most recent run {verb} and nothing has succeeded since",
            )

    if last_success is None:
        return (
            SourceHealth.NEVER_COLLECTED,
            "no live run has ever succeeded, so there is no position to resume from",
        )

    if expected_success_after is None:
        return SourceHealth.HEALTHY, "nothing was due since the last success"

    if last_success >= utc(expected_success_after):
        return SourceHealth.HEALTHY, "collection succeeded after the last expected window"

    return (
        SourceHealth.STALE,
        f"last success {last_success.isoformat()} predates the expected window "
        f"{utc(expected_success_after).isoformat()}",
    )


__all__ = [
    "INCIDENT_KINDS",
    "ComponentHeartbeat",
    "CursorAdvance",
    "CursorConflict",
    "EventKind",
    "HeartbeatStatus",
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
    "resolve_health",
    "utc",
]
