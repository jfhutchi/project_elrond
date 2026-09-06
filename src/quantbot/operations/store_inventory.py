"""Dependency-free, immutable SQLite evidence shared by local and portable inventory.

No runtime imports, database construction, migrations or sidecar writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

UNKNOWN = "UNKNOWN"


def redact(value: Any, environ: Mapping[str, str]) -> Any:
    """Exclude arbitrary diagnostic payloads and mask credentials in permitted text too."""
    if isinstance(value, dict):
        return {key: redact(item, environ) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, environ) for item in value]
    if not isinstance(value, str):
        return value
    for name, secret in environ.items():
        if re.search(r"SECRET|TOKEN|PASSWORD|API_KEY|ACCOUNT_ID", name, re.I) and secret:
            value = value.replace(secret, "<redacted>")
    value = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"']+", "<redacted-url>", value)
    return re.sub(
        r"(?i)(password|secret|token|api[_-]?key|credential)\s*[=:]\s*[^\s,;]+",
        r"\1=<redacted>",
        value,
    )


def file_evidence(path: Path) -> dict[str, object]:
    result: dict[str, object] = {"path": str(path)}
    try:
        stat = path.stat()
        result.update(
            state="PRESENT",
            file_id=f"{stat.st_dev}:{stat.st_ino}" if stat.st_ino else UNKNOWN,
            bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
            created_at=datetime.fromtimestamp(stat.st_ctime, UTC).isoformat()
            if os.name == "nt"
            else UNKNOWN,
        )
    except FileNotFoundError:
        result["state"] = "ABSENT"
    except OSError:
        result["state"] = "UNREADABLE"
    return result


def _signature(path: Path) -> tuple[tuple[int, int, int, int] | None, ...]:
    signature: list[tuple[int, int, int, int] | None] = []
    for member in (path, Path(str(path) + "-wal"), Path(str(path) + "-journal")):
        try:
            stat = member.stat()
            signature.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            signature.append(None)
    return tuple(signature)


def _rows(
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    *,
    order: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]] | None:
    # All identifiers are fixed by this module, never taken from CLI/DB content.
    available = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    if not set(columns) <= available or (order and order not in available):
        return None
    selection = ", ".join(f'"{column}"' for column in columns)
    # Resolve timestamp ties deterministically; row insertion order is not provenance.
    tie_breakers = ", ".join(f'"{column}"' for column in columns if column != order)
    ordering = f' ORDER BY "{order}" DESC' if order else " ORDER BY "
    ordering += (", " if order and tie_breakers else "") + tie_breakers
    return [
        dict(zip(columns, (_json_value(value) for value in row), strict=True))
        for row in connection.execute(
            f'SELECT {selection} FROM "{table}"{ordering} LIMIT ?', (limit,)
        )
    ]


def _json_value(value: Any) -> Any:
    return {"blob_sha256": hashlib.sha256(value).hexdigest()} if isinstance(value, bytes) else value


def digest(value: Any) -> str:
    """Canonical content hash; not a signature or proof that recorded claims are true."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_value)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def plausible_store(store: Mapping[str, Any]) -> bool:
    return store.get("evidence_state") == "READABLE_SNAPSHOT" and (
        any(
            store.get(field)
            for field in (
                "latest_run",
                "deployments",
                "latest_account_snapshot",
                "runtime_observations",
                "ingestion_cursors",
                "latest_market_observation",
                "qualification_rows",
            )
        )
        or any((store.get("history_counts") or {}).values())
    )


def inspect_store(
    path: Path,
    *,
    enrich: Callable[[sqlite3.Connection, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Read fixed evidence columns from any schema; no migrations or secret/detail dumps.

    Immutable SQLite prevents even sidecar creation. It is NOT safe for a WAL's base file:
    refuse nonempty WAL/journal files, and discard evidence if source metadata changes.
    This bounded snapshot still does not prove a live process is absent or grant authority.
    """
    result: dict[str, Any] = dict(file_evidence(path))
    result.update(backend="sqlite", authority=UNKNOWN, evidence_state=UNKNOWN)
    if result["state"] != "PRESENT":
        return result
    try:
        before = _signature(path)
        if any(item and item[2] for item in before[1:]):
            result["evidence_state"] = "SIDECARS_REQUIRE_CONSISTENT_SNAPSHOT"
            return result
        uri = path.as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            # Bound corrupt/hostile or unexpectedly large queries.
            budget = 0

            def interrupted() -> int:
                nonlocal budget
                budget += 1
                return int(budget > 20_000)

            connection.set_progress_handler(interrupted, 1000)
            result["schema"] = _rows(connection, "schema_version", ("version",), limit=2)
            result["revision"] = _rows(connection, "alembic_version", ("version_num",), limit=2)
            result["store_identity"] = _rows(
                connection, "schema_version", ("store_identity",), limit=2
            )
            result["latest_run"] = _rows(
                connection,
                "runs",
                ("run_id", "status", "started_at", "finished_at"),
                order="started_at",
                limit=1,
            )
            result["last_successful_cycle_at"] = None
            if result["latest_run"] is not None:
                row = connection.execute(
                    "SELECT max(finished_at) FROM runs WHERE status='SUCCEEDED'"
                ).fetchone()
                result["last_successful_cycle_at"] = _json_value(row[0]) if row else None
            result["latest_market_observation"] = _rows(
                connection, "bars", ("timestamp", "provider"), order="timestamp", limit=1
            )
            result["deployments"] = _rows(
                connection,
                "strategy_deployments",
                (
                    "strategy_id",
                    "version",
                    "git_commit",
                    "configuration_hash",
                    "deployment_timestamp",
                ),
                order="deployment_timestamp",
                limit=20,
            )
            result["kill_switch"] = _rows(
                connection, "kill_switch_state", ("engaged", "updated_at"), limit=2
            )
            result["latest_reconciliation"] = _rows(
                connection,
                "reconciliation_runs",
                ("reconciliation_id", "run_id", "status", "completed_at"),
                order="completed_at",
                limit=1,
            )
            accounts = _rows(
                connection,
                "account_snapshots",
                ("account_id", "captured_at", "run_id"),
                order="captured_at",
                limit=1,
            )
            result["latest_account_snapshot"] = (
                [
                    {
                        "account_hash": hashlib.sha256(str(row["account_id"]).encode()).hexdigest(),
                        "captured_at": row["captured_at"],
                        "run_id": row["run_id"],
                    }
                    for row in accounts or []
                ]
                if accounts is not None
                else None
            )
            # Account identity alone cannot detect a divergent snapshot on that account.
            snapshots = _rows(
                connection,
                "account_snapshots",
                (
                    "snapshot_id",
                    "run_id",
                    "account_id",
                    "captured_at",
                    "cash",
                    "buying_power",
                    "equity",
                    "currency",
                ),
                order="captured_at",
                limit=1,
            )
            result["account_snapshot_content"] = (
                [
                    {
                        "snapshot_id": row["snapshot_id"],
                        "run_id": row["run_id"],
                        "captured_at": row["captured_at"],
                        "sha256": digest(row),
                    }
                    for row in snapshots
                ]
                if snapshots is not None
                else None
            )
            result["qualification_rows"] = _rows(
                connection,
                "qualification_days",
                ("strategy_id", "trading_date", "qualified"),
                order="trading_date",
                limit=40,
            )
            result["latest_session_observation"] = _rows(
                connection,
                "session_observations",
                ("observed_at", "outcome"),
                order="observed_at",
                limit=1,
            )
            result["last_successful_observation"] = None
            if result["latest_session_observation"] is not None:
                row = connection.execute(
                    "SELECT max(observed_at) FROM session_observations WHERE outcome='HEALTHY'"
                ).fetchone()
                result["last_successful_observation"] = _json_value(row[0]) if row else None
            result["ingestion_cursors"] = _rows(
                connection,
                "ingestion_cursors",
                ("source_id", "stream_key", "last_success_at", "last_observation_at"),
            )
            cursor_content = _rows(
                connection,
                "ingestion_cursors",
                (
                    "source_id",
                    "stream_key",
                    "cursor_value",
                    "last_success_at",
                    "last_observation_at",
                    "updated_at",
                    "version",
                ),
            )
            result["ingestion_cursor_content"] = (
                [
                    {
                        "source_id": row["source_id"],
                        "stream_key": row["stream_key"],
                        "sha256": digest(row),
                    }
                    for row in cursor_content
                ]
                if cursor_content is not None
                else None
            )
            result["latest_ingestion_runs"] = _rows(
                connection,
                "ingestion_runs",
                ("ingestion_run_id", "source_id", "mode", "status", "started_at", "finished_at"),
                order="started_at",
                limit=20,
            )
            result["ingestion_sources"] = _rows(
                connection,
                "ingestion_sources",
                (
                    "source_id",
                    "provider",
                    "source_kind",
                    "configuration_hash",
                    "enabled",
                    "created_at",
                    "updated_at",
                ),
            )
            result["runtime_observations"] = _rows(
                connection,
                "runtime_deployments",
                (
                    "observation_id",
                    "host_id",
                    "instance_id",
                    "role",
                    "git_commit",
                    "trading_mode",
                    "store_identity",
                    "schema_version",
                    "strategy_id",
                    "configuration_hash",
                    "process_started_at",
                    "observed_at",
                ),
                order="observed_at",
                limit=100,
            )
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            result["history_counts"] = {
                table: connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                if table in tables
                else None
                for table in (
                    "runs",
                    "broker_orders",
                    "fills",
                    "account_snapshots",
                    "positions",
                    "reconciliation_runs",
                    "reconciliation_diffs",
                    "qualification_days",
                    "research_records",
                    "bars",
                    "closes",
                    "ingestion_runs",
                    "runtime_deployments",
                )
            }
            result["runtime"] = UNKNOWN
            result["ingestion"] = UNKNOWN
            if enrich is not None:
                enrich(connection, result)
            result["evidence_state"] = "READABLE_SNAPSHOT"
        finally:
            connection.close()
        # Detect differences beyond bounded latest-row summaries, without exposing contents.
        # Equality proves byte equality at capture, not authority; layout-only changes differ too.
        result["content_sha256"] = UNKNOWN
        result["content_hash_scope"] = "entire SQLite base file; maximum 512 MiB"
        if path.stat().st_size <= 512 * 1024 * 1024:
            content_hash = hashlib.sha256()
            with path.open("rb") as source:
                remaining = 512 * 1024 * 1024 + 1
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        result["content_sha256"] = content_hash.hexdigest()
                        break
                    content_hash.update(chunk)
                    remaining -= len(chunk)
        if before != _signature(path):
            return {
                **file_evidence(path),
                "authority": UNKNOWN,
                "evidence_state": "CHANGED_DURING_INSPECTION",
            }
    except (OSError, sqlite3.Error, ValueError, TypeError):
        # Never print exception text: SQLite messages can include values/schema payloads.
        return {**file_evidence(path), "authority": UNKNOWN, "evidence_state": "UNREADABLE"}
    return result


def discover_stores(root: Path, *, max_entries: int = 100_000) -> dict[str, Any]:
    """Explicit, bounded search; failures and pruned scope are part of the evidence."""
    excluded = {
        ".git",
        ".venv",
        "node_modules",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "__pycache__",
    }
    found: list[str] = []
    errors: list[str] = []
    scanned = 0

    def failed(error: OSError) -> None:
        errors.append(str(error.filename))

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed):
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in excluded and not (Path(directory) / name).is_symlink()
        )
        scanned += len(files) + len(dirs)
        if scanned > max_entries:
            break
        found.extend(
            str(Path(directory) / name)
            for name in sorted(files)
            if Path(name).suffix.lower() in (".db", ".sqlite", ".sqlite3")
        )
    return {
        "root": str(root),
        "candidates": found,
        "unreadable": errors,
        "entries_seen": scanned,
        "limit_reached": scanned > max_entries,
        "excluded_directory_names": sorted(excluded),
        "follows_symlinks": False,
    }
