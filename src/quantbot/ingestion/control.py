"""Durable ingestion control inside one caller-owned transaction (#198).

The design constraint that shapes everything here: **a cursor must not be able to advance past
data that was not committed.** The control plane therefore does not own a transaction. It reads
and writes through the session the caller is already using to store observations, so the
checkpoint and the rows it points past commit together or roll back together. There is no
window in which one exists without the other, and no code path that could open one.

Three further rules are enforced by shape rather than by discipline:

1. **Only `complete_run` can advance a cursor.** `fail_run` and `abandon_run` do not take a
   `CursorAdvance`, so a failed partial batch cannot claim ground it did not cover. There is no
   public `advance_cursor` for the same reason -- a checkpoint is a consequence of a run that
   succeeded, not an independent operation.
2. **Only a `LIVE` run can write `last_success_at`.** The field is derived from the run's mode
   inside `complete_run`; `CursorAdvance` has no such field to pass. A source caught up by
   backfill therefore still reports `NEVER_COLLECTED` or `STALE`, which is the truth.
3. **A stale checkpoint is refused, not merged.** `CursorAdvance.expected_version` must match
   the stored version. Two collectors racing on one stream is where a cursor silently goes
   backwards, and the skipped window is never noticed.

**This module never reads the trading kill switch.** An engaged kill switch halts order
submission. It does not say market data may not be collected, and stopping collection because
trading is halted would destroy exactly the observations needed to understand the halt.
`test_ingestion_never_consults_the_trading_kill_switch` asserts the independence rather than
leaving it as an intention.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

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
    SourceStatus,
    resolve_health,
    utc,
)
from quantbot.storage.database import decode_utc, encode_utc
from quantbot.storage.schema import (
    component_heartbeats,
    ingestion_cursors,
    ingestion_events,
    ingestion_runs,
    ingestion_sources,
)


def _json(detail: Mapping[str, object] | str) -> str:
    payload = {"note": detail} if isinstance(detail, str) else dict(detail)
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _optional_time(value: object) -> datetime | None:
    return None if value is None else decode_utc(str(value))


class IngestionControlPlane:
    """Read and write ingestion control state inside the caller's transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # --- sources -------------------------------------------------------------------------

    def register_source(
        self,
        *,
        source_id: str,
        provider: str,
        source_kind: str,
        configuration_hash: str,
        now: datetime,
        enabled: bool = True,
    ) -> IngestionSource:
        """Register a source, or re-register it under a changed configuration.

        A changed `configuration_hash` is recorded as a `CONFIGURATION_CHANGED` event rather
        than applied silently: it changes what "this stream" means, and a cursor carried across
        it points into a different dataset than the one it was measured against.
        """
        moment = utc(now)
        existing = self.get_source(source_id)
        if existing is None:
            source = IngestionSource(
                source_id=source_id,
                provider=provider,
                source_kind=source_kind,
                configuration_hash=configuration_hash,
                enabled=enabled,
                created_at=moment,
                updated_at=moment,
            )
            self._session.execute(
                ingestion_sources.insert().values(
                    source_id=source.source_id,
                    provider=source.provider,
                    source_kind=source.source_kind,
                    configuration_hash=source.configuration_hash,
                    enabled=source.enabled,
                    created_at=encode_utc(source.created_at),
                    updated_at=encode_utc(source.updated_at),
                )
            )
            self.record_event(
                source_id=source_id,
                event_kind=EventKind.SOURCE_REGISTERED,
                severity=Severity.INFO,
                now=moment,
                detail={"provider": provider, "source_kind": source_kind},
            )
            return source

        if (existing.provider, existing.source_kind) != (provider, source_kind):
            raise IngestionError(
                f"{source_id} is registered as {existing.provider}/{existing.source_kind}; "
                "a different provider or kind is a different source"
            )
        if existing.configuration_hash == configuration_hash:
            return existing

        self._session.execute(
            update(ingestion_sources)
            .where(ingestion_sources.c.source_id == source_id)
            .values(configuration_hash=configuration_hash, updated_at=encode_utc(moment))
        )
        self.record_event(
            source_id=source_id,
            event_kind=EventKind.CONFIGURATION_CHANGED,
            severity=Severity.WARNING,
            now=moment,
            detail={"from": existing.configuration_hash, "to": configuration_hash},
        )
        return existing.model_copy(
            update={"configuration_hash": configuration_hash, "updated_at": moment}
        )

    def set_enabled(
        self, source_id: str, *, enabled: bool, now: datetime, reason: str
    ) -> IngestionSource:
        """Pause or resume a source. Recorded, because a silent pause is indistinguishable
        from a crash to everyone who did not perform it."""
        source = self._require_source(source_id)
        moment = utc(now)
        if source.enabled == enabled:
            return source
        self._session.execute(
            update(ingestion_sources)
            .where(ingestion_sources.c.source_id == source_id)
            .values(enabled=enabled, updated_at=encode_utc(moment))
        )
        self.record_event(
            source_id=source_id,
            event_kind=EventKind.OPERATOR_RESUMED if enabled else EventKind.OPERATOR_PAUSED,
            severity=Severity.INFO,
            now=moment,
            detail={"reason": reason},
        )
        return source.model_copy(update={"enabled": enabled, "updated_at": moment})

    def get_source(self, source_id: str) -> IngestionSource | None:
        row = (
            self._session.execute(
                select(ingestion_sources).where(ingestion_sources.c.source_id == source_id)
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _source_from_row(row)

    def list_sources(self) -> list[IngestionSource]:
        rows = (
            self._session.execute(
                select(ingestion_sources).order_by(ingestion_sources.c.source_id)
            )
            .mappings()
            .all()
        )
        return [_source_from_row(row) for row in rows]

    # --- runs ----------------------------------------------------------------------------

    def next_run_id(self, source_id: str, now: datetime) -> str:
        """A deterministic, collision-free id for one attempt at one recorded instant.

        Two attempts at the same instant are two facts, not one. A cycle replayed under a frozen
        clock -- which the integration suite does, and which a same-second retry does in
        production -- must record a *second* attempt rather than fail on a duplicate id. So the
        ordinal comes from what is already stored rather than from a random component, and the
        id stays reproducible.

        The first caller found this immediately: `f"{source_id}:{timestamp}"` looked obviously
        unique until a cycle was re-run.
        """
        stamp = encode_utc(utc(now))
        rows = self._session.execute(
            select(ingestion_runs.c.ingestion_run_id).where(
                ingestion_runs.c.source_id == source_id,
                ingestion_runs.c.started_at == stamp,
            )
        ).all()
        return f"{source_id}:{stamp}:{len(rows)}"

    def begin_run(
        self,
        *,
        ingestion_run_id: str,
        source_id: str,
        mode: RunMode,
        now: datetime,
        requested_start: str | None = None,
        requested_end: str | None = None,
        detail: Mapping[str, object] | str = "",
    ) -> IngestionRun:
        """Open a collection attempt. Refuses a disabled source and a duplicate id."""
        source = self._require_source(source_id)
        if not source.enabled:
            raise IngestionError(f"{source_id} is disabled; enable it before collecting")
        if self.get_run(ingestion_run_id) is not None:
            raise IngestionError(f"ingestion run {ingestion_run_id} already exists")
        run = IngestionRun(
            ingestion_run_id=ingestion_run_id,
            source_id=source_id,
            mode=mode,
            status=RunStatus.RUNNING,
            started_at=utc(now),
            requested_start=requested_start,
            requested_end=requested_end,
        )
        self._session.execute(ingestion_runs.insert().values(**_run_values(run, detail)))
        return run

    def complete_run(
        self,
        ingestion_run_id: str,
        *,
        counts: RecordCounts,
        now: datetime,
        cursor: CursorAdvance | None = None,
        detail: Mapping[str, object] | str = "",
    ) -> IngestionRun:
        """Close a run as succeeded and, in the same transaction, move its checkpoint.

        The checkpoint move is here and nowhere else. Committing observations and advancing
        past them is one atomic fact, and splitting it into two calls would create the window
        where a crash leaves the cursor ahead of the data.
        """
        run = self._require_running(ingestion_run_id)
        moment = utc(now)
        finished = run.model_copy(
            update={
                "status": RunStatus.SUCCEEDED,
                "finished_at": moment,
                "counts": counts,
            }
        )
        self._session.execute(
            update(ingestion_runs)
            .where(ingestion_runs.c.ingestion_run_id == ingestion_run_id)
            .values(**_run_values(finished, detail))
        )
        if cursor is not None:
            self._advance(finished, cursor, now=moment)
        return finished

    def fail_run(
        self,
        ingestion_run_id: str,
        *,
        error_code: str,
        now: datetime,
        counts: RecordCounts | None = None,
        detail: Mapping[str, object] | str = "",
    ) -> IngestionRun:
        """Close a run as failed and open a provider-failure incident.

        Deliberately takes no `CursorAdvance`. A run that failed did not establish that it
        covered its range, and a partial batch that could checkpoint would skip whatever it
        never reached.
        """
        run = self._require_running(ingestion_run_id)
        moment = utc(now)
        finished = run.model_copy(
            update={
                "status": RunStatus.FAILED,
                "finished_at": moment,
                "counts": counts or run.counts,
                "error_code": error_code,
            }
        )
        self._session.execute(
            update(ingestion_runs)
            .where(ingestion_runs.c.ingestion_run_id == ingestion_run_id)
            .values(**_run_values(finished, detail))
        )
        self.record_event(
            source_id=run.source_id,
            event_kind=EventKind.PROVIDER_FAILURE,
            severity=Severity.CRITICAL,
            now=moment,
            detail={"error_code": error_code, "ingestion_run_id": ingestion_run_id},
        )
        return finished

    def abandon_stale_runs(
        self, *, older_than: datetime, now: datetime, source_id: str | None = None
    ) -> tuple[IngestionRun, ...]:
        """Close runs that started before `older_than` and never finished.

        A process killed mid-batch leaves a `RUNNING` row nothing will ever close from the
        inside, and an unclosed run is otherwise indistinguishable from one still working. Only
        something that outlived the process can make that call, which is why it is a separate
        operation and not a timeout inside the run.
        """
        cutoff = encode_utc(utc(older_than))
        statement = select(ingestion_runs).where(
            ingestion_runs.c.status == RunStatus.RUNNING.value,
            ingestion_runs.c.started_at < cutoff,
        )
        if source_id is not None:
            # Scoped on request, because "stale" is a per-source judgement: a collector that
            # legitimately runs for hours is not evidence about one that runs for seconds.
            statement = statement.where(ingestion_runs.c.source_id == source_id)
        rows = self._session.execute(statement).mappings().all()
        moment = utc(now)
        abandoned: list[IngestionRun] = []
        for row in rows:
            run = _run_from_row(row)
            finished = run.model_copy(
                update={
                    "status": RunStatus.ABANDONED,
                    "finished_at": moment,
                    "error_code": "RUN_NEVER_FINISHED",
                }
            )
            self._session.execute(
                update(ingestion_runs)
                .where(ingestion_runs.c.ingestion_run_id == run.ingestion_run_id)
                .values(**_run_values(finished, "closed by a later observer"))
            )
            self.record_event(
                source_id=run.source_id,
                event_kind=EventKind.RUN_ABANDONED,
                severity=Severity.CRITICAL,
                now=moment,
                detail={"ingestion_run_id": run.ingestion_run_id},
            )
            abandoned.append(finished)
        return tuple(abandoned)

    def get_run(self, ingestion_run_id: str) -> IngestionRun | None:
        row = (
            self._session.execute(
                select(ingestion_runs).where(
                    ingestion_runs.c.ingestion_run_id == ingestion_run_id
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _run_from_row(row)

    def latest_run(self, source_id: str, *, mode: RunMode | None = None) -> IngestionRun | None:
        statement = select(ingestion_runs).where(ingestion_runs.c.source_id == source_id)
        if mode is not None:
            statement = statement.where(ingestion_runs.c.mode == mode.value)
        row = (
            self._session.execute(
                statement.order_by(
                    ingestion_runs.c.started_at.desc(),
                    ingestion_runs.c.ingestion_run_id.desc(),
                ).limit(1)
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _run_from_row(row)

    def runs_for(self, source_id: str, *, limit: int = 50) -> list[IngestionRun]:
        rows = (
            self._session.execute(
                select(ingestion_runs)
                .where(ingestion_runs.c.source_id == source_id)
                .order_by(
                    ingestion_runs.c.started_at.desc(),
                    ingestion_runs.c.ingestion_run_id.desc(),
                )
                .limit(limit)
            )
            .mappings()
            .all()
        )
        return [_run_from_row(row) for row in rows]

    # --- cursors -------------------------------------------------------------------------

    def cursor(self, source_id: str, stream_key: str) -> IngestionCursor | None:
        row = (
            self._session.execute(
                select(ingestion_cursors).where(
                    ingestion_cursors.c.source_id == source_id,
                    ingestion_cursors.c.stream_key == stream_key,
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _cursor_from_row(row)

    def cursors_for(self, source_id: str) -> list[IngestionCursor]:
        rows = (
            self._session.execute(
                select(ingestion_cursors)
                .where(ingestion_cursors.c.source_id == source_id)
                .order_by(ingestion_cursors.c.stream_key)
            )
            .mappings()
            .all()
        )
        return [_cursor_from_row(row) for row in rows]

    def reset_cursor(
        self, source_id: str, stream_key: str, *, now: datetime, reason: str
    ) -> None:
        """Delete a checkpoint so the next run starts over, and say so durably.

        A cursor reset silently re-reads a range that was already collected. The re-read is
        harmless -- storage is idempotent -- but a reset nobody recorded makes a later gap
        impossible to explain.
        """
        self._require_source(source_id)
        self._session.execute(
            ingestion_cursors.delete().where(
                ingestion_cursors.c.source_id == source_id,
                ingestion_cursors.c.stream_key == stream_key,
            )
        )
        self.record_event(
            source_id=source_id,
            stream_key=stream_key,
            event_kind=EventKind.CURSOR_RESET,
            severity=Severity.WARNING,
            now=now,
            detail={"reason": reason},
        )

    def _advance(self, run: IngestionRun, advance: CursorAdvance, *, now: datetime) -> None:
        existing = self.cursor(run.source_id, advance.stream_key)
        # `last_success_at` is derived from the run, never supplied. A backfill cannot make a
        # stopped live feed look current, whatever it passes.
        success_at = now if run.mode is RunMode.LIVE else None

        if existing is None:
            if advance.expected_version is not None:
                raise CursorConflict(
                    f"{run.source_id}/{advance.stream_key} has no checkpoint, but the advance "
                    f"expects version {advance.expected_version}"
                )
            self._session.execute(
                ingestion_cursors.insert().values(
                    source_id=run.source_id,
                    stream_key=advance.stream_key,
                    cursor_value=advance.cursor_value,
                    last_observation_at=(
                        None
                        if advance.last_observation_at is None
                        else encode_utc(advance.last_observation_at)
                    ),
                    last_success_at=None if success_at is None else encode_utc(success_at),
                    updated_at=encode_utc(now),
                    version=1,
                )
            )
            return

        if advance.expected_version != existing.version:
            raise CursorConflict(
                f"{run.source_id}/{advance.stream_key} is at version {existing.version}; "
                f"the advance expects {advance.expected_version}. Another collector moved it, "
                "and merging the two would skip whatever lies between them"
            )

        # A backfill has no claim on the live freshness marker, so it keeps whatever a previous
        # live run established rather than clearing it.
        self._session.execute(
            update(ingestion_cursors)
            .where(
                ingestion_cursors.c.source_id == run.source_id,
                ingestion_cursors.c.stream_key == advance.stream_key,
                ingestion_cursors.c.version == existing.version,
            )
            .values(
                cursor_value=advance.cursor_value,
                last_observation_at=(
                    None
                    if advance.last_observation_at is None
                    else encode_utc(advance.last_observation_at)
                ),
                last_success_at=(
                    encode_utc(success_at)
                    if success_at is not None
                    else (
                        None
                        if existing.last_success_at is None
                        else encode_utc(existing.last_success_at)
                    )
                ),
                updated_at=encode_utc(now),
                version=existing.version + 1,
            )
        )

    # --- events and heartbeats -----------------------------------------------------------

    def record_event(
        self,
        *,
        source_id: str,
        event_kind: EventKind,
        severity: Severity,
        now: datetime,
        stream_key: str | None = None,
        detail: Mapping[str, object] | str = "",
    ) -> IngestionEvent:
        moment = utc(now)
        event = IngestionEvent(
            event_id=f"{source_id}:{event_kind.value}:{encode_utc(moment)}:"
            f"{stream_key or '-'}:{self._event_ordinal(source_id, moment)}",
            source_id=source_id,
            stream_key=stream_key,
            event_kind=event_kind,
            severity=severity,
            occurred_at=moment,
            detail=_json(detail),
        )
        self._session.execute(
            ingestion_events.insert().values(
                event_id=event.event_id,
                source_id=event.source_id,
                stream_key=event.stream_key,
                event_kind=event.event_kind.value,
                severity=event.severity.value,
                occurred_at=encode_utc(event.occurred_at),
                resolved_at=None,
                detail_json=_json(detail),
            )
        )
        return event

    def _event_ordinal(self, source_id: str, moment: datetime) -> int:
        """Disambiguate two events for one source in the same recorded instant."""
        rows = self._session.execute(
            select(ingestion_events.c.event_id).where(
                ingestion_events.c.source_id == source_id,
                ingestion_events.c.occurred_at == encode_utc(moment),
            )
        ).all()
        return len(rows)

    def open_incidents(
        self, source_id: str, *, event_kind: EventKind | None = None
    ) -> list[IngestionEvent]:
        statement = select(ingestion_events).where(
            ingestion_events.c.source_id == source_id,
            ingestion_events.c.resolved_at.is_(None),
            ingestion_events.c.event_kind.in_([kind.value for kind in INCIDENT_KINDS]),
        )
        if event_kind is not None:
            statement = statement.where(ingestion_events.c.event_kind == event_kind.value)
        rows = (
            self._session.execute(statement.order_by(ingestion_events.c.occurred_at))
            .mappings()
            .all()
        )
        return [_event_from_row(row) for row in rows]

    def resolve_incidents(
        self, source_id: str, *, now: datetime, detail: str = ""
    ) -> tuple[IngestionEvent, ...]:
        """Close every open incident for a source, and say what observed the recovery.

        Resolution is an observation, never an inference from silence: a collector that stopped
        emitting must not be able to close its own incident by going quiet.
        """
        moment = utc(now)
        open_now = self.open_incidents(source_id)
        if not open_now:
            return ()
        self._session.execute(
            update(ingestion_events)
            .where(
                ingestion_events.c.source_id == source_id,
                ingestion_events.c.resolved_at.is_(None),
                ingestion_events.c.event_kind.in_([kind.value for kind in INCIDENT_KINDS]),
            )
            .values(resolved_at=encode_utc(moment))
        )
        self.record_event(
            source_id=source_id,
            event_kind=EventKind.RECOVERED,
            severity=Severity.INFO,
            now=moment,
            detail={"resolved": len(open_now), "note": detail},
        )
        return tuple(event.model_copy(update={"resolved_at": moment}) for event in open_now)

    def heartbeat(
        self,
        *,
        component_id: str,
        instance_id: str,
        status: HeartbeatStatus,
        now: datetime,
        last_success_at: datetime | None = None,
        detail: Mapping[str, object] | str = "",
    ) -> ComponentHeartbeat:
        moment = utc(now)
        beat = ComponentHeartbeat(
            heartbeat_id=f"{component_id}:{instance_id}:{encode_utc(moment)}",
            component_id=component_id,
            instance_id=instance_id,
            observed_at=moment,
            status=status,
            last_success_at=None if last_success_at is None else utc(last_success_at),
            detail=_json(detail),
        )
        self._session.execute(
            component_heartbeats.insert().values(
                heartbeat_id=beat.heartbeat_id,
                component_id=beat.component_id,
                instance_id=beat.instance_id,
                observed_at=encode_utc(beat.observed_at),
                status=beat.status.value,
                last_success_at=(
                    None if beat.last_success_at is None else encode_utc(beat.last_success_at)
                ),
                detail_json=_json(detail),
            )
        )
        return beat

    def latest_heartbeat(self, component_id: str) -> ComponentHeartbeat | None:
        row = (
            self._session.execute(
                select(component_heartbeats)
                .where(component_heartbeats.c.component_id == component_id)
                .order_by(
                    component_heartbeats.c.observed_at.desc(),
                    component_heartbeats.c.heartbeat_id.desc(),
                )
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return ComponentHeartbeat(
            heartbeat_id=str(row["heartbeat_id"]),
            component_id=str(row["component_id"]),
            instance_id=str(row["instance_id"]),
            observed_at=decode_utc(str(row["observed_at"])),
            status=HeartbeatStatus(str(row["status"])),
            last_success_at=_optional_time(row["last_success_at"]),
            detail=str(row["detail_json"]),
        )

    # --- status --------------------------------------------------------------------------

    def status(
        self,
        source_id: str,
        *,
        stream_key: str,
        expected_success_after: datetime | None,
        record_incident: bool = False,
        now: datetime | None = None,
    ) -> SourceStatus:
        """Classify a source, optionally opening or resolving a durable incident.

        `record_incident` is opt-in and needs `now`, because merely asking a question should
        not write history. When it is on, a stale or failed source opens one incident and keeps
        it open -- repeated observation does not stack duplicates -- and a recovery resolves it.
        """
        source = self._require_source(source_id)
        cursor = self.cursor(source_id, stream_key)
        latest = self.latest_run(source_id)
        health, reason = resolve_health(
            source=source,
            cursor=cursor,
            latest_run=latest,
            expected_success_after=expected_success_after,
        )
        if record_incident:
            if now is None:
                raise IngestionError("recording an incident needs the time it was observed")
            self._reconcile_incident(
                source_id, stream_key=stream_key, health=health, reason=reason, now=now
            )
        return SourceStatus(
            source_id=source_id,
            health=health,
            enabled=source.enabled,
            last_success_at=None if cursor is None else cursor.last_success_at,
            last_observation_at=None if cursor is None else cursor.last_observation_at,
            expected_success_after=expected_success_after,
            latest_run_id=None if latest is None else latest.ingestion_run_id,
            latest_run_status=None if latest is None else latest.status,
            open_incidents=len(self.open_incidents(source_id)),
            reason=reason,
        )

    def _reconcile_incident(
        self,
        source_id: str,
        *,
        stream_key: str,
        health: object,
        reason: str,
        now: datetime,
    ) -> None:
        from quantbot.ingestion.models import SourceHealth

        if health is SourceHealth.HEALTHY:
            self.resolve_incidents(source_id, now=now, detail="collection is current again")
            return
        if health in {SourceHealth.DISABLED, SourceHealth.NEVER_COLLECTED}:
            # Neither is an incident. A disabled source is not expected to collect, and one
            # that has never run has nothing to have stopped doing.
            return
        if self.open_incidents(source_id, event_kind=EventKind.STALE_DETECTED):
            return
        if health is SourceHealth.FAILED and self.open_incidents(
            source_id, event_kind=EventKind.PROVIDER_FAILURE
        ):
            return
        self.record_event(
            source_id=source_id,
            stream_key=stream_key,
            event_kind=EventKind.STALE_DETECTED,
            severity=Severity.CRITICAL,
            now=now,
            detail={"reason": reason},
        )

    # --- internals -----------------------------------------------------------------------

    def _require_source(self, source_id: str) -> IngestionSource:
        source = self.get_source(source_id)
        if source is None:
            raise IngestionError(f"ingestion source {source_id} is not registered")
        return source

    def _require_running(self, ingestion_run_id: str) -> IngestionRun:
        run = self.get_run(ingestion_run_id)
        if run is None:
            raise IngestionError(f"ingestion run {ingestion_run_id} does not exist")
        if run.status is not RunStatus.RUNNING:
            raise IngestionError(
                f"ingestion run {ingestion_run_id} is already {run.status.value}; a finished "
                "run cannot be finished again"
            )
        return run


def _run_values(run: IngestionRun, detail: Mapping[str, object] | str) -> dict[str, object]:
    return {
        "ingestion_run_id": run.ingestion_run_id,
        "source_id": run.source_id,
        "mode": run.mode.value,
        "started_at": encode_utc(run.started_at),
        "finished_at": None if run.finished_at is None else encode_utc(run.finished_at),
        "status": run.status.value,
        "requested_start": run.requested_start,
        "requested_end": run.requested_end,
        "records_seen": run.counts.seen,
        "records_inserted": run.counts.inserted,
        "records_duplicate": run.counts.duplicate,
        "records_rejected": run.counts.rejected,
        "error_code": run.error_code,
        "detail_json": _json(detail),
    }


def _source_from_row(row: RowMapping) -> IngestionSource:
    return IngestionSource(
        source_id=str(row["source_id"]),
        provider=str(row["provider"]),
        source_kind=str(row["source_kind"]),
        configuration_hash=str(row["configuration_hash"]),
        enabled=bool(row["enabled"]),
        created_at=decode_utc(str(row["created_at"])),
        updated_at=decode_utc(str(row["updated_at"])),
    )


def _run_from_row(row: RowMapping) -> IngestionRun:
    return IngestionRun(
        ingestion_run_id=str(row["ingestion_run_id"]),
        source_id=str(row["source_id"]),
        mode=RunMode(str(row["mode"])),
        status=RunStatus(str(row["status"])),
        started_at=decode_utc(str(row["started_at"])),
        finished_at=_optional_time(row["finished_at"]),
        requested_start=None if row["requested_start"] is None else str(row["requested_start"]),
        requested_end=None if row["requested_end"] is None else str(row["requested_end"]),
        counts=RecordCounts(
            seen=int(row["records_seen"]),
            inserted=int(row["records_inserted"]),
            duplicate=int(row["records_duplicate"]),
            rejected=int(row["records_rejected"]),
        ),
        error_code=None if row["error_code"] is None else str(row["error_code"]),
        detail=str(row["detail_json"]),
    )


def _cursor_from_row(row: RowMapping) -> IngestionCursor:
    return IngestionCursor(
        source_id=str(row["source_id"]),
        stream_key=str(row["stream_key"]),
        cursor_value=None if row["cursor_value"] is None else str(row["cursor_value"]),
        last_observation_at=_optional_time(row["last_observation_at"]),
        last_success_at=_optional_time(row["last_success_at"]),
        updated_at=decode_utc(str(row["updated_at"])),
        version=int(row["version"]),
    )


def _event_from_row(row: RowMapping) -> IngestionEvent:
    return IngestionEvent(
        event_id=str(row["event_id"]),
        source_id=str(row["source_id"]),
        stream_key=None if row["stream_key"] is None else str(row["stream_key"]),
        event_kind=EventKind(str(row["event_kind"])),
        severity=Severity(str(row["severity"])),
        occurred_at=decode_utc(str(row["occurred_at"])),
        resolved_at=_optional_time(row["resolved_at"]),
        detail=str(row["detail_json"]),
    )


__all__ = ["IngestionControlPlane"]
