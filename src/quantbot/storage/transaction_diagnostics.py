"""Opt-in, bounded SQLite timing. No SQL, values, extra queries, or logging under locks.

Coverage is limited to explicitly observed Database instances. Absence of a
record cannot exclude a transaction in another process or an unobserved engine.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import UUID, uuid4
from weakref import WeakSet

from sqlalchemy import Engine, event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

_KEY = "quantbot_transaction_diagnostic"


def _quiet(function: Callable[..., Any]) -> Callable[..., None]:
    @wraps(function)
    def safe(*args: Any, **kwargs: Any) -> None:
        try:
            function(*args, **kwargs)
        except BaseException:
            pass  # Instrumentation must not change an operation or its original exception.

    return safe


class _Timing:
    def __init__(self, read_only: bool, path: Path, store_identity: str | None) -> None:
        self.started = monotonic()
        self.at = datetime.now(UTC).isoformat()
        self.read_only = read_only
        self.database_id = hashlib.sha256(str(path.resolve()).casefold().encode()).hexdigest()
        self.store_identity = store_identity
        self.thread_id = threading.get_ident()
        self.callsite = "unknown"
        frame = sys._getframe(1)
        for _ in range(12):
            module = str(frame.f_globals.get("__name__", ""))
            if module not in {__name__, "quantbot.storage.database", "contextlib"}:
                self.callsite = f"{module}:{frame.f_code.co_name}:{frame.f_lineno}"
                break
            if frame.f_back is None:
                break
            frame = frame.f_back
        del frame
        self.stage = "connection"
        self.error_stage: str | None = None
        self.sqlite_errorcode: int | None = None
        self.first_connection_elapsed_ms: float | None = None
        self.close_started: float | None = None
        self.epochs: list[dict[str, Any]] = []
        self.epoch: dict[str, Any] | None = None
        self.epochs_dropped = 0
        self.statement_started = 0.0
        self.statement_parent = "body"
        self.operation = "OTHER"
        self.connections: list[dict[str, Any]] = []

    def current(self) -> dict[str, Any]:
        if self.epoch is None:
            self.epoch = dict(
                begin_attempted=None,
                writer_confirmed=False,
                write_observed=False,
                writer_confirmed_at=None,
                writer_held_lower_bound_ms=0.0,
                statement_count=0,
                statement_ms=0.0,
                begin_ms=0.0,
                longest_statement_ms=0.0,
                commit_ms=0.0,
                rollback_ms=0.0,
                outcome="open",
            )
            if len(self.epochs) < 16:
                self.epochs.append(self.epoch)
            else:
                self.epochs_dropped += 1
        return self.epoch

    def attach(self, connection: Connection) -> None:
        info = connection.info
        if not any(info is previous for previous in self.connections):
            self.connections.append(info)
        info[_KEY] = self
        if self.first_connection_elapsed_ms is None:
            # Includes caller work before lazy acquisition; not pure pool wait time.
            self.first_connection_elapsed_ms = (monotonic() - self.started) * 1000
        self.current()
        self.stage = "body"

    def error(self, error: BaseException, stage: str | None = None) -> None:
        if self.error_stage is None:
            self.error_stage = stage or self.stage
            original = getattr(error, "orig", error)
            code = getattr(original, "sqlite_errorcode", None)
            self.sqlite_errorcode = code if isinstance(code, int) else None

    def begin_commit(self) -> None:
        self.stage = "commit"
        if self.epoch is not None:
            self.epoch.setdefault("commit_started", monotonic())

    def begin_rollback(self) -> None:
        self.stage = "rollback"
        if self.epoch is not None:
            self.epoch.setdefault("rollback_started", monotonic())

    @_quiet
    def mark_closing(self) -> None:
        self.close_started = monotonic()

    def end_epoch(self, outcome: str) -> None:
        if self.epoch is not None:
            now = monotonic()
            for phase in ("commit", "rollback"):
                started = self.epoch.pop(phase + "_started", None)
                if started is not None:
                    self.epoch[phase + "_ms"] += (now - started) * 1000
            confirmed = self.epoch.pop("writer_mono", None)
            if confirmed is not None:
                self.epoch["writer_held_lower_bound_ms"] = (now - confirmed) * 1000
            self.epoch["outcome"] = outcome
            self.epoch = None
        self.stage = "body"


class TransactionObserver:
    """One observer per process; nested contexts defer output until the outer closes."""

    def __init__(
        self,
        *,
        expires_at_utc: datetime,
        sink: Callable[[dict[str, Any]], None],
        process_epoch: str | None = None,
        role: str | None = None,
        max_records: int = 128,
        slow_seconds: float = 1.0,
    ) -> None:
        self.expires_at_utc = expires_at_utc
        self.sink = sink
        self.role = role if role in {"guardian", "outcomes", "trader"} else "unspecified"
        try:
            self.process_epoch = str(UUID(process_epoch)) if process_epoch else str(uuid4())
        except ValueError:
            self.process_epoch = str(uuid4())
        self.started_at = datetime.now(UTC)
        self.started_mono = monotonic()
        self.max_records = max(0, min(128, max_records))
        self.slow_seconds = max(0.0, slow_seconds)
        self.records = 0
        self._record_slots = deque(range(self.max_records))
        self.local = threading.local()
        self.engines: WeakSet[Engine] = WeakSet()

    @property
    def active(self) -> bool:
        try:
            return (
                bool(self._record_slots)
                and self.expires_at_utc.tzinfo is not None
                and self.started_at < self.expires_at_utc
                and (self.expires_at_utc - self.started_at).total_seconds() <= 1200
                and datetime.now(UTC) < self.expires_at_utc
                and 0 <= monotonic() - self.started_mono < 1200
            )
        except BaseException:
            return False

    def begin(self, path: Path, read_only: bool, store_identity: str | None) -> _Timing | None:
        if not self.active:
            return None
        state = _Timing(read_only, path, store_identity)
        if not hasattr(self.local, "stack"):
            self.local.stack = []
            self.local.pending = []
        self.local.stack.append(state)
        return state

    @_quiet
    def watch_engine(self, engine: Engine) -> None:
        if engine in self.engines:
            return
        self.engines.add(engine)

        @_quiet
        def before(connection: Connection, cursor: Any, statement: str, *args: Any) -> None:
            state = connection.info.get(_KEY)
            if state is None:
                return
            state.statement_started = monotonic()
            state.statement_parent = state.stage
            state.stage = "statement"
            words = statement.lstrip()[:40].upper().split()
            operation = words[0] if words else "OTHER"
            state.operation = (
                operation
                if operation in {"BEGIN", "SELECT", "INSERT", "UPDATE", "DELETE", "REPLACE"}
                else "OTHER"
            )
            epoch = state.current()
            if operation == "BEGIN":
                epoch["begin_attempted"] = (
                    "IMMEDIATE" if len(words) > 1 and words[1] == "IMMEDIATE" else "DEFERRED"
                )

        @_quiet
        def after(connection: Connection, *args: Any) -> None:
            state = connection.info.get(_KEY)
            if state is None:
                return
            elapsed = (monotonic() - state.statement_started) * 1000
            epoch = state.current()
            epoch["statement_count"] += 1
            epoch["statement_ms"] += elapsed
            epoch["longest_statement_ms"] = max(epoch["longest_statement_ms"], elapsed)
            if state.operation == "BEGIN":
                epoch["begin_ms"] += elapsed
            writing = state.operation in {"INSERT", "UPDATE", "DELETE", "REPLACE"}
            epoch["write_observed"] |= writing
            immediate = state.operation == "BEGIN" and epoch["begin_attempted"] == "IMMEDIATE"
            driver = connection.connection.driver_connection
            if (immediate or writing) and getattr(driver, "in_transaction", False):
                if not epoch["writer_confirmed"]:
                    epoch["writer_mono"] = monotonic()
                    epoch["writer_confirmed_at"] = datetime.now(UTC).isoformat()
                epoch["writer_confirmed"] = True
            state.stage = state.statement_parent

        @_quiet
        def failed(context: Any) -> None:
            if context.connection is not None:
                state = context.connection.info.get(_KEY)
                if state is not None:
                    if state.stage == "statement":
                        elapsed = (monotonic() - state.statement_started) * 1000
                        epoch = state.current()
                        epoch["statement_count"] += 1
                        epoch["statement_ms"] += elapsed
                        epoch["longest_statement_ms"] = max(epoch["longest_statement_ms"], elapsed)
                        if state.operation == "BEGIN":
                            epoch["begin_ms"] += elapsed
                    elif state.stage == "commit" and state.epoch is not None:
                        started = state.epoch.pop("commit_started", None)
                        if started is not None:
                            state.epoch["commit_ms"] += (monotonic() - started) * 1000
                    state.error(context.original_exception)

        @_quiet
        def rollback(connection: Connection) -> None:
            state = connection.info.get(_KEY)
            if state is not None:
                state.begin_rollback()

        event.listen(engine, "before_cursor_execute", before)
        event.listen(engine, "after_cursor_execute", after)
        event.listen(engine, "handle_error", failed)
        event.listen(engine, "rollback", rollback)

    @_quiet
    def watch_session(self, session: Session, state: _Timing) -> None:
        event.listen(session, "after_begin", _quiet(lambda s, t, c: state.attach(c)))
        event.listen(session, "before_commit", _quiet(lambda s: state.begin_commit()))
        event.listen(session, "after_commit", _quiet(lambda s: state.end_epoch("commit")))
        event.listen(session, "after_rollback", _quiet(lambda s: state.end_epoch("rollback")))

    @_quiet
    def watch_connection(self, connection: Connection, state: _Timing) -> None:
        state.attach(connection)

    def finish(self, state: _Timing) -> None:
        ended = monotonic()
        state.end_epoch("rollback" if state.error_stage else "close")
        for info in state.connections:
            if info.get(_KEY) is state:
                info.pop(_KEY, None)
        self.local.stack.remove(state)
        total_ms = (ended - state.started) * 1000
        permitted = False
        if total_ms >= self.slow_seconds * 1000 or state.error_stage is not None:
            try:
                self._record_slots.popleft()  # Atomic bounded permit; no added application lock.
                permitted = True
            except IndexError:
                pass
        if permitted:
            self.records += 1
            close_ms = (ended - state.close_started) * 1000 if state.close_started else 0.0
            phase_ms = sum(e["commit_ms"] + e["rollback_ms"] for e in state.epochs)
            self.local.pending.append(
                dict(
                    event="SQLITE_TRANSACTION_TIMING",
                    at=state.at,
                    ended_at=datetime.now(UTC).isoformat(),
                    pid=os.getpid(),
                    process_epoch=self.process_epoch,
                    observer_started_at=self.started_at.isoformat(),
                    role=self.role,
                    thread_id=state.thread_id,
                    database_id=state.database_id,
                    store_identity=state.store_identity,
                    callsite=state.callsite,
                    read_only=state.read_only,
                    total_ms=total_ms,
                    close_ms=close_ms,
                    body_ms=max(0.0, total_ms - close_ms - phase_ms),
                    first_connection_elapsed_ms=state.first_connection_elapsed_ms,
                    epochs=state.epochs,
                    epochs_dropped=state.epochs_dropped,
                    error_stage=state.error_stage,
                    sqlite_errorcode=state.sqlite_errorcode,
                    coverage="explicit_database_instances_only",
                )
            )
        if not self.local.stack:
            pending, self.local.pending = self.local.pending, []
            for record in pending:
                try:
                    self.sink(record)
                except BaseException:
                    pass


@contextmanager
def observe_transaction(
    observer: TransactionObserver | None,
    path: Path,
    *,
    read_only: bool = False,
    store_identity: str | None = None,
) -> Iterator[_Timing | None]:
    state = None
    try:
        if observer is not None:
            state = observer.begin(path, read_only, store_identity)
    except BaseException:
        pass
    try:
        yield state
    finally:
        if state is not None and observer is not None:
            try:
                error = sys.exc_info()[1]
                if error is not None:
                    state.error(error)
                observer.finish(state)
            except BaseException:
                pass


def stdout_transaction_observer(
    *,
    expires_at_utc: datetime,
    process_epoch: str,
    role: str,
    max_records: int = 128,
    slow_seconds: float = 1.0,
) -> TransactionObserver | None:
    try:
        if role not in {"guardian", "outcomes", "trader"}:
            return None
        observer = TransactionObserver(
            expires_at_utc=expires_at_utc,
            process_epoch=process_epoch,
            role=role,
            max_records=max_records,
            slow_seconds=slow_seconds,
            sink=lambda record: print(json.dumps(record, separators=(",", ":")), flush=True),
        )
        return observer if observer.active else None
    except BaseException:
        return None
