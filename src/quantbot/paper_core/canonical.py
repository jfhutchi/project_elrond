"""Explicit metadata-only snapshot of canonical holds and permanent trial charges."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from quantbot.paper_core.store import CoreStore, put, table
from quantbot.research.manifest import content_id
from quantbot.storage import StateConflictError

TRIAL_FIELDS = (
    "search_id",
    "actor",
    "actor_version",
    "candidates_evaluated",
    "dataset",
    "start_date",
    "end_date",
    "data_role",
    "started_at",
    "finished_at",
    "configuration_hash",
)


def snapshot_canonical_metadata(path: Path, datasets: tuple[str, ...]) -> dict[str, Any]:
    if not path.is_absolute() or not path.is_file():
        raise ValueError("existing absolute canonical source required")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        windows = [dict(r) for r in connection.execute("SELECT * FROM hypothesis_data_windows")]
        holds = [
            dict(r)
            for r in connection.execute(
                "SELECT dataset_id, "
                "json_extract(document_json,'$.holdout_window.dataset') AS dataset, "
                "json_extract(document_json,'$.holdout_window.start') AS start_date, "
                "json_extract(document_json,'$.holdout_window.end') AS end_date "
                "FROM learner_datasets"
            )
        ]
        trials = [
            dict(r)
            for r in connection.execute("SELECT " + ",".join(TRIAL_FIELDS) + " FROM search_runs")
        ]
        identity = dict(connection.execute("SELECT * FROM schema_version").fetchone())
        return {
            "source_path": str(path),
            "source_identity": identity,
            "captured_at": datetime.now(UTC),
            "canonical_datasets": datasets,
            "windows": windows,
            "holdouts": holds,
            "trials": trials,
            "scope": "CANONICAL_METADATA_ONLY_NO_FEATURE_OR_TARGET_PAYLOADS",
        }
    finally:
        connection.close()


def import_canonical_metadata(session: Session, document: dict[str, Any]) -> str:
    identity = content_id(document)
    put(session, "core_canonical_sources", identity, document)
    pointer = table("core_canonical_current")
    if session.execute(select(pointer.c.id)).first():
        session.execute(update(pointer).values(source_id=identity))
    else:
        session.execute(pointer.insert().values(id="current", source_id=identity))
    for supplied in document["trials"]:
        row = {name: supplied[name] for name in TRIAL_FIELDS}
        target = table("search_runs")
        old = (
            session.execute(select(target).where(target.c.search_id == row["search_id"]))
            .mappings()
            .one_or_none()
        )
        if old is not None:
            if any(old[name] != row[name] for name in TRIAL_FIELDS):
                raise StateConflictError("CANONICAL_TRIAL_METADATA_CONFLICT")
        else:
            session.execute(
                target.insert().values(
                    **row,
                    detail_json=json.dumps(
                        {
                            "source": "IMPORTED_CANONICAL_METADATA",
                            "source_snapshot_id": identity,
                            "original_detail": "RETAINED_IN_IMMUTABLE_SOURCE_NOT_HYDRATED",
                        }
                    ),
                )
            )
    return identity


def refresh_canonical_metadata(store: CoreStore) -> str:
    from quantbot.paper_core.authority import commission_in_session

    with store.database.transaction() as session:
        commission = commission_in_session(store, session)
        source = session.execute(
            select(table("core_canonical_sources").c.payload).where(
                table("core_canonical_sources").c.id == commission.canonical_metadata_id
            )
        ).scalar_one()
    original = json.loads(source)
    snapshot = snapshot_canonical_metadata(
        Path(original["source_path"]), tuple(original["canonical_datasets"])
    )
    if snapshot["source_identity"] != original["source_identity"]:
        raise StateConflictError("CANONICAL_SOURCE_IDENTITY_OR_SCHEMA_CHANGED")
    with store.database.transaction() as session:
        return import_canonical_metadata(session, snapshot)
