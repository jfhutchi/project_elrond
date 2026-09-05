"""Recovery evidence without constructing a runtime or opening a writable store (#190).

This is an inventory, not a lease service. A path, UUID, recent row, free local lock or
reachable worker cannot establish global authority over an account. Never pick a winner.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from quantbot.operations.checkout import checkout_evidence
from quantbot.storage.schema import SCHEMA_VERSION
from quantbot.storage.target import DatabaseTargetError, coerce_target, resolve_database_target

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
            file_id=f"{stat.st_dev}:{stat.st_ino}",
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
    ordering = f' ORDER BY "{order}" DESC' if order else ""
    return [
        dict(zip(columns, row, strict=True))
        for row in connection.execute(
            f'SELECT {selection} FROM "{table}"{ordering} LIMIT ?', (limit,)
        )
    ]


def inspect_store(path: Path, *, now: datetime, repo: Path, intended: str) -> dict[str, Any]:
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
                result["last_successful_cycle_at"] = row[0] if row else None
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
                result["last_successful_observation"] = row[0] if row else None
            result["ingestion_cursors"] = _rows(
                connection,
                "ingestion_cursors",
                ("source_id", "stream_key", "last_success_at", "last_observation_at"),
            )
            result["runtime"] = UNKNOWN
            result["ingestion"] = UNKNOWN
            # Reuse the existing readers against this exact read-only connection. Older
            # stores still expose historical evidence above without needing today's schema.
            if result["schema"] == [{"version": SCHEMA_VERSION}]:
                from quantbot.ingestion import IngestionControlPlane, ingestion_status
                from quantbot.operations.deployment import DeploymentRegistry, summarize

                engine = create_engine("sqlite://", creator=lambda: connection)
                try:
                    with Session(engine) as session:
                        result["runtime"] = summarize(
                            DeploymentRegistry(session).status(
                                now=now,
                                intended_commit=intended,
                                repo=repo,
                            )
                        )
                        ingestion = ingestion_status(IngestionControlPlane(session), now=now)
                        # Incident detail is arbitrary provider text, unnecessary for inventory.
                        sources = ingestion["sources"]
                        if isinstance(sources, list):
                            for source in sources:
                                source.pop("open_incidents", None)
                        result["ingestion"] = ingestion
                finally:
                    engine.dispose()
            result["evidence_state"] = "READABLE_SNAPSHOT"
        finally:
            connection.close()
        if before != _signature(path):
            return {
                **file_evidence(path),
                "authority": UNKNOWN,
                "evidence_state": "CHANGED_DURING_INSPECTION",
            }
    except (OSError, sqlite3.Error, SQLAlchemyError, ValueError, TypeError):
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


def inventory(
    *,
    repo: Path,
    candidates: Sequence[str] = (),
    scan_roots: Sequence[str] = (),
    intended_commit: str | None = None,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
    include_host: bool = False,
) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    moment = now or datetime.now(UTC)
    repo = repo.expanduser().resolve()
    checkout = checkout_evidence(repo, intended_commit)
    configured: Path | None = None
    target_error: str | None = None
    try:
        target = resolve_database_target(env)
        target.require_enabled()
        configured = target.path.resolve()  # runtime paths resolve against process cwd
    except DatabaseTargetError as error:
        target_error = error.code
    paths: set[Path] = set()
    if configured is not None:
        paths.add(configured)
    scans = [discover_stores(Path(root).expanduser().resolve()) for root in scan_roots]
    discovered = [path for scan in scans for path in scan["candidates"]]
    candidate_errors = []
    for candidate in [*candidates, *discovered]:
        try:
            candidate_target = coerce_target(candidate)
            candidate_target.require_enabled()
            paths.add(candidate_target.path.resolve())
        except DatabaseTargetError as error:
            candidate_errors.append(error.code)
    stores = [
        inspect_store(path, now=moment, repo=repo, intended=str(checkout["intended_commit"]))
        for path in sorted(paths)
    ]
    plausible = [
        store
        for store in stores
        if store.get("latest_run")
        or store.get("deployments")
        or store.get("latest_account_snapshot")
    ]
    # Aliased paths/hard links are one physical candidate; copied lineage is not.
    physical_candidates = {store.get("file_id", store["path"]) for store in plausible}
    authority = "AMBIGUOUS" if len(physical_candidates) > 1 else UNKNOWN
    reasons = [
        "GLOBAL_COORDINATOR_AUTHORITY_UNVERIFIED",
        "PI_STATE_UNKNOWN",
        "OTHER_HOST_RETIREMENT_UNVERIFIED",
    ]
    if authority == "AMBIGUOUS":
        reasons.append("MULTIPLE_PLAUSIBLE_STORES")
    if not plausible:
        reasons.append("NO_PLAUSIBLE_STORE_IN_SUPPLIED_CANDIDATES")
    if target_error or candidate_errors:
        reasons.append("DATABASE_TARGET_UNRESOLVED")
    configured_store = next((store for store in stores if store["path"] == str(configured)), None)
    if configured_store is None or configured_store.get("evidence_state") != "READABLE_SNAPSHOT":
        reasons.append("CONFIGURED_STORE_NOT_READABLE")
    elif configured_store.get("schema") != [{"version": SCHEMA_VERSION}]:
        reasons.append("CONFIGURED_STORE_SCHEMA_MISMATCH")
    if not configured_store or not configured_store.get("kill_switch"):
        reasons.append("KILL_SWITCH_UNKNOWN")
    elif configured_store["kill_switch"][0]["engaged"] != 0:
        reasons.append("KILL_SWITCH_ENGAGED")
    reasons.append("BROKER_RECONCILIATION_NOT_PROBED")
    if checkout["checkout_drift"] != "MATCH" or checkout["dirty"] is not False:
        reasons.append("CHECKOUT_NOT_VERIFIED_AT_INTENDED_COMMIT")
    mode = env.get("TRADING_MODE", "PAPER")
    broker_mode = env.get("BROKER_ENVIRONMENT", "PAPER")
    acknowledged = env.get("LIVE_TRADING_ACKNOWLEDGED", "false").lower()
    if mode != "PAPER" or broker_mode != "PAPER" or acknowledged not in ("false", "0", "off", "no"):
        reasons.append("PAPER_ONLY_CONFIGURATION_REQUIRED")
    lock = Path(env.get("QUANTBOT_LOCK_PATH", "quantbot.lock")).expanduser().resolve()
    locks = [
        {**file_evidence(item), "owner": UNKNOWN, "held": UNKNOWN}
        for item in (lock, lock.with_suffix(".daemon.lock"), lock.with_suffix(".protection.lock"))
    ]
    result: dict[str, Any] = {
        "ok": False,
        "command": "deployment-status",
        "as_of": moment.isoformat(),
        "scope": "local checkout and explicitly supplied store candidates; no remote probe",
        "host": {
            "hostname": platform.node(),
            "os": platform.system(),
            "version": platform.version(),
            "architecture": platform.machine(),
        },
        "checkout": checkout,
        "configured_database": str(configured) if configured else UNKNOWN,
        "configured_database_source": (
            "QUANTBOT_DATABASE_URL"
            if env.get("QUANTBOT_DATABASE_URL")
            else "QUANTBOT_DB_PATH"
            if env.get("QUANTBOT_DB_PATH")
            else "default relative to cwd"
        ),
        "configuration_path": str(
            Path(env.get("QUANTBOT_CONFIG", "config/strategy-v1.yaml")).expanduser().resolve()
        ),
        "database_target_error": target_error,
        "candidate_errors": candidate_errors,
        "stores": stores,
        "candidate_scans": scans,
        "store_authority": authority,
        "coordinator_authority": UNKNOWN,
        "pi_state": UNKNOWN,
        "locks": locks,
        "configured_mode": mode if mode in ("PAPER", "LIVE") else UNKNOWN,
        "configured_broker_environment": broker_mode
        if broker_mode in ("PAPER", "LIVE")
        else UNKNOWN,
        "runtime_mode": UNKNOWN,
        "process_id": os.getpid(),
        "process_role": "TOOL",
        "worker_availability": UNKNOWN,
        "workers_have_coordinator_authority": False,
        "startup_preflight": {
            "allowed": False,
            "blockers": reasons,
            "note": "Recovery report only; no global fence exists in local locks.",
        },
        "note": "A readable or configured store is a candidate, not proof of authority. "
        "Null fields mean absent/unavailable evidence. Row lists are bounded, not totals. "
        "Store lineage survives copies; mtime never selects a winner. "
        "Lock files contain no PID; existence does not establish a live owner.",
    }
    if include_host:
        from quantbot.operations.host_inventory import host_inventory

        result["os_inventory"] = host_inventory()
        # supervisor.py writes to cwd/logs; no QUANTBOT_LOGS_DIR resolver exists.
        log_dir = Path("logs").resolve()
        try:
            result["logs"] = [
                file_evidence(item) for item in sorted(log_dir.iterdir()) if item.is_file()
            ][:100]
        except OSError:
            result["logs"] = UNKNOWN
    return redact(result, env)  # type: ignore[no-any-return]
