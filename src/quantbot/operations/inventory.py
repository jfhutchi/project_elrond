"""Recovery evidence without constructing a runtime or opening a writable store (#190).

This is an inventory, not a lease service. A path, UUID, recent row, free local lock or
reachable worker cannot establish global authority over an account. Never pick a winner.
"""

from __future__ import annotations

import os
import platform
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from quantbot.operations.checkout import checkout_evidence
from quantbot.operations.store_inventory import (
    discover_stores,
    file_evidence,
    plausible_store,
    redact,
)
from quantbot.operations.store_inventory import (
    inspect_store as read_store,
)
from quantbot.storage.schema import SCHEMA_VERSION
from quantbot.storage.target import DatabaseTargetError, coerce_target, resolve_database_target

UNKNOWN = "UNKNOWN"


def inspect_store(path: Path, *, now: datetime, repo: Path, intended: str) -> dict[str, Any]:
    def enrich(connection: sqlite3.Connection, result: dict[str, Any]) -> None:
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

    try:
        return read_store(path, enrich=enrich)
    except SQLAlchemyError:
        return {**file_evidence(path), "authority": UNKNOWN, "evidence_state": "UNREADABLE"}


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
    plausible = [store for store in stores if plausible_store(store)]
    # Aliased paths/hard links are one physical candidate; copied lineage is not.
    physical_candidates = {
        (store["file_id"] if store.get("file_id") not in (None, "UNKNOWN") else store["path"])
        for store in plausible
    }
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
        "report_version": 1,
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
