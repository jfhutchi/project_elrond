"""Additive experiment evidence and exclusive workflow; never an operational DB."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel, Field

from quantbot.learner.experiment_contract import Digest, ExperimentManifest
from quantbot.learner.models import LearnerModel, Text

ModelT = TypeVar("ModelT", bound=BaseModel)
SCHEMA = "ADAPTIVE_PAPER_STORE_V1"
TABLES = {"metadata", "events", "states", "leases", "outbox", "conflicts"}
TRANSITIONS = {
    "REGISTERED": {"WAITING_FOR_COVERAGE", "OBSERVING", "FAULTED"},
    "WAITING_FOR_COVERAGE": {"OBSERVING", "INSUFFICIENT_EVIDENCE", "FAULTED"},
    "OBSERVING": {
        "WAITING_FOR_COVERAGE",
        "FIT_ELIGIBLE",
        "CANDIDATE_SHADOW",
        "EVALUATING",
        "INSUFFICIENT_EVIDENCE",
        "FAULTED",
    },
    "FIT_ELIGIBLE": {"FIT_REGISTERED", "INSUFFICIENT_EVIDENCE", "FAULTED"},
    "FIT_REGISTERED": {"CANDIDATE_SHADOW", "REJECTED", "FAULTED"},
    "CANDIDATE_SHADOW": {"ACTIVATION_REQUIRED", "EVALUATING", "FIT_ELIGIBLE", "FAULTED"},
    "ACTIVATION_REQUIRED": {"ACTIVE_PAPER", "EVALUATING", "FAULTED"},
    "ACTIVE_PAPER": {"EVALUATING", "FIT_ELIGIBLE", "INSUFFICIENT_EVIDENCE", "FAULTED"},
    "EVALUATING": {
        "FIT_ELIGIBLE",
        "ACTIVATION_REQUIRED",
        "COMPLETED",
        "INSUFFICIENT_EVIDENCE",
        "REJECTED",
        "FAULTED",
    },
    "COMPLETED": set(),
    "INSUFFICIENT_EVIDENCE": set(),
    "REJECTED": set(),
    "FAULTED": set(),
}


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware UTC timestamp required")
    return value.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return _aware(value).isoformat(timespec="microseconds")


def _digest(document: str) -> str:
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


class ConsentNotice(LearnerModel):
    """Communication evidence only; cannot grant model or order authority."""

    experiment_id: Text
    consent_id: Text
    recorded_at: datetime
    grants_execution: Literal[False] = False


class ExperimentState(LearnerModel):
    experiment_id: Text
    manifest_hash: Digest
    state: Text = "REGISTERED"
    revision: int = Field(default=0, ge=0)
    reason: Text = "REGISTERED_NOT_ACTIVATED"
    changed_at: datetime
    prospective_start_at: datetime
    consent_recorded: bool = False
    activation_id: str | None = None


class StoredEvent(LearnerModel):
    record_id: Digest
    experiment_id: Text
    kind: Text
    recorded_at: datetime
    document: Text

    def decode(self, model: type[ModelT]) -> ModelT:
        if model.__name__ != self.kind or _digest(self.document) != self.record_id:
            raise ValueError("EVIDENCE_KIND_OR_HASH_MISMATCH")
        return model.model_validate_json(self.document)


class OutboxIntent(LearnerModel):
    intent_id: Text
    experiment_id: Text
    document: Text
    state: Literal["PENDING", "CLAIMED", "ACKNOWLEDGED"]
    owner: str | None


class ExperimentStore:
    def __init__(self, path: Path, *, create: bool = False, read_only: bool = False) -> None:
        if read_only and create:
            raise ValueError("READ_ONLY_CANNOT_CREATE_STORE")
        self.read_only = read_only
        if path.is_symlink():
            raise ValueError("store symlink refused")
        self.path = path.resolve()
        new = not self.path.exists()
        if new and not create:
            raise FileNotFoundError("explicit experiment store creation required")
        if new:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=False)
        mode = "rw" if new else "ro"
        with sqlite3.connect(self.path.as_uri() + f"?mode={mode}", uri=True) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if tables:
                if tables != TABLES or db.execute(
                    "SELECT value FROM metadata WHERE key='schema'"
                ).fetchone() != (SCHEMA,):
                    raise ValueError("UNRELATED_OR_UNSUPPORTED_DATABASE")
            elif not new:
                raise ValueError("UNRELATED_EMPTY_DATABASE")
            else:
                db.executescript("""
                    CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE events(experiment_id TEXT NOT NULL,
                      idempotency_key TEXT NOT NULL, kind TEXT NOT NULL, recorded_at TEXT NOT NULL,
                      document TEXT NOT NULL, digest TEXT NOT NULL,
                      PRIMARY KEY(experiment_id, idempotency_key));
                    CREATE TABLE states(experiment_id TEXT PRIMARY KEY, document TEXT NOT NULL);
                    CREATE TABLE leases(experiment_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                      expires_at TEXT NOT NULL);
                    CREATE TABLE outbox(intent_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL,
                      document TEXT NOT NULL, state TEXT NOT NULL, owner TEXT);
                    CREATE TABLE conflicts(experiment_id TEXT NOT NULL,
                      idempotency_key TEXT NOT NULL, original_digest TEXT NOT NULL,
                      rejected_digest TEXT NOT NULL);
                """)
                db.execute("INSERT INTO metadata VALUES ('schema', ?)", (SCHEMA,))

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        if write and self.read_only:
            raise ValueError("READ_ONLY_EXPERIMENT_STORE")
        mode = "rw" if write else "ro"
        db = sqlite3.connect(self.path.as_uri() + f"?mode={mode}", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _state(db: sqlite3.Connection, experiment_id: str) -> ExperimentState:
        row = db.execute(
            "SELECT document FROM states WHERE experiment_id=?", (experiment_id,)
        ).fetchone()
        if row is None:
            raise ValueError("EXPERIMENT_NOT_REGISTERED")
        return ExperimentState.model_validate_json(row[0])

    @staticmethod
    def _save_state(db: sqlite3.Connection, state: ExperimentState) -> None:
        db.execute(
            "INSERT OR REPLACE INTO states VALUES (?, ?)",
            (state.experiment_id, state.model_dump_json()),
        )

    def state(self, experiment_id: str) -> ExperimentState:
        with self._transaction(write=False) as db:
            return self._state(db, experiment_id)

    def append(self, record: BaseModel, *, idempotency_key: str) -> bool:
        if not idempotency_key:
            raise ValueError("idempotency identity required")
        verified = type(record).model_validate(record.model_dump())
        values = verified.model_dump(mode="json")
        experiment_id = str(values["experiment_id"])
        recorded = datetime.fromisoformat(
            str(values.get("recorded_at", values.get("registered_at")))
        )
        document = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = _digest(document)
        conflict = False
        with self._transaction() as db:
            old = db.execute(
                "SELECT digest FROM events WHERE experiment_id=? AND idempotency_key=?",
                (experiment_id, idempotency_key),
            ).fetchone()
            if old is not None:
                if old[0] == digest:
                    return False
                state = self._state(db, experiment_id)
                db.execute(
                    "INSERT INTO conflicts VALUES (?, ?, ?, ?)",
                    (experiment_id, idempotency_key, old[0], digest),
                )
                self._save_state(
                    db,
                    state.model_copy(
                        update={
                            "state": "FAULTED",
                            "reason": "CONFLICTING_IMMUTABLE_EVIDENCE",
                            "revision": state.revision + 1,
                        }
                    ),
                )
                conflict = True
            else:
                if isinstance(verified, ExperimentManifest):
                    try:
                        self._state(db, experiment_id)
                    except ValueError:
                        self._save_state(
                            db,
                            ExperimentState(
                                experiment_id=experiment_id,
                                manifest_hash=content_hash(verified),
                                changed_at=recorded,
                                prospective_start_at=verified.prospective_start_at,
                            ),
                        )
                    else:
                        raise ValueError("MANIFEST_ALREADY_REGISTERED")
                else:
                    state = self._state(db, experiment_id)
                    if isinstance(verified, ConsentNotice):
                        self._save_state(db, state.model_copy(update={"consent_recorded": True}))
                db.execute(
                    "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        experiment_id,
                        idempotency_key,
                        type(record).__name__,
                        _stamp(recorded),
                        document,
                        digest,
                    ),
                )
        if conflict:
            raise ValueError("CONFLICTING_IMMUTABLE_EVIDENCE")
        return True

    def read_as_of(
        self, experiment_id: str, now: datetime, *, maximum_records: int | None = None
    ) -> tuple[StoredEvent, ...]:
        if maximum_records is not None and not 1 <= maximum_records <= 10000:
            raise ValueError("bounded evidence read requires 1..10000 records")
        with self._transaction(write=False) as db:
            rows = db.execute(
                "SELECT * FROM events WHERE experiment_id=? AND recorded_at<=? "
                "ORDER BY recorded_at, rowid LIMIT ?",
                (experiment_id, _stamp(now), maximum_records + 1 if maximum_records else -1),
            ).fetchall()
            if maximum_records is not None and len(rows) > maximum_records:
                raise ValueError("EXPERIMENT_EVIDENCE_BOUND_EXCEEDED")
            result = []
            for row in rows:
                if _digest(row["document"]) != row["digest"]:
                    raise ValueError("EVIDENCE_HASH_MISMATCH")
                result.append(
                    StoredEvent(
                        record_id=row["digest"],
                        experiment_id=experiment_id,
                        kind=row["kind"],
                        recorded_at=row["recorded_at"],
                        document=row["document"],
                    )
                )
            return tuple(result)

    def transition(
        self, experiment_id: str, *, expected_revision: int, target: str, reason: str, now: datetime
    ) -> ExperimentState:
        now = _aware(now)
        with self._transaction() as db:
            state = self._state(db, experiment_id)
            if state.revision != expected_revision:
                raise ValueError("STALE_STATE_REVISION")
            if target == "ACTIVE_PAPER" and state.activation_id is None:
                raise ValueError("EXACT_ARTIFACT_ACTIVATION_REQUIRED")
            if target not in TRANSITIONS[state.state] or now < state.changed_at:
                raise ValueError("INVALID_STATE_TRANSITION")
            successor = ExperimentState.model_validate(
                state.model_dump()
                | {
                    "state": target,
                    "reason": reason,
                    "changed_at": now,
                    "revision": state.revision + 1,
                }
            )
            document = successor.model_dump_json()
            db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                (
                    experiment_id,
                    f"transition:{successor.revision}",
                    "ExperimentState",
                    _stamp(now),
                    document,
                    _digest(document),
                ),
            )
            self._save_state(db, successor)
            return successor

    def acquire_lease(
        self, experiment_id: str, *, owner: str, now: datetime, duration: timedelta
    ) -> bool:
        if not owner or not timedelta(0) < duration <= timedelta(minutes=3):
            raise ValueError("bounded owner lease required")
        with self._transaction() as db:
            self._state(db, experiment_id)
            row = db.execute(
                "SELECT owner, expires_at FROM leases WHERE experiment_id=?", (experiment_id,)
            ).fetchone()
            if row is not None and row[0] != owner and row[1] > _stamp(now):
                return False
            db.execute(
                "INSERT OR REPLACE INTO leases VALUES (?, ?, ?)",
                (experiment_id, owner, _stamp(now + duration)),
            )
            return True

    def enqueue_intent(self, experiment_id: str, *, intent_id: str, document: str) -> None:
        with self._transaction() as db:
            self._state(db, experiment_id)
            old = db.execute(
                "SELECT experiment_id, document FROM outbox WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if old is not None:
                if tuple(old) != (experiment_id, document):
                    raise ValueError("OUTBOX_IDENTITY_CONFLICT")
                return
            db.execute(
                "INSERT INTO outbox VALUES (?, ?, ?, 'PENDING', NULL)",
                (intent_id, experiment_id, document),
            )

    def claim_intent(self, intent_id: str, *, owner: str, now: datetime) -> str | None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT o.* FROM outbox o JOIN leases l USING(experiment_id) "
                "WHERE o.intent_id=? AND l.owner=? AND l.expires_at>?",
                (intent_id, owner, _stamp(now)),
            ).fetchone()
            if row is None or row["state"] != "PENDING":
                return None
            db.execute(
                "UPDATE outbox SET state='CLAIMED', owner=? WHERE intent_id=?", (owner, intent_id)
            )
            return str(row["document"])

    def get_intent(self, intent_id: str) -> OutboxIntent | None:
        with self._transaction(write=False) as db:
            row = db.execute("SELECT * FROM outbox WHERE intent_id=?", (intent_id,)).fetchone()
            return None if row is None else OutboxIntent.model_validate(dict(row))

    def pending_intents(self, experiment_id: str) -> tuple[OutboxIntent, ...]:
        with self._transaction(write=False) as db:
            rows = db.execute(
                "SELECT * FROM outbox WHERE experiment_id=? "
                "AND state!='ACKNOWLEDGED' ORDER BY intent_id",
                (experiment_id,),
            ).fetchall()
            return tuple(OutboxIntent.model_validate(dict(row)) for row in rows)

    def acknowledge_intent(self, intent_id: str, *, owner: str) -> None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT state, owner FROM outbox WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if row is None or row[1] != owner or row[0] not in {"CLAIMED", "ACKNOWLEDGED"}:
                raise ValueError("OUTBOX_ACKNOWLEDGEMENT_OWNER_MISMATCH")
            db.execute("UPDATE outbox SET state='ACKNOWLEDGED' WHERE intent_id=?", (intent_id,))


def content_hash(manifest: ExperimentManifest) -> str:
    from quantbot.research.manifest import content_id

    return content_id(manifest)
