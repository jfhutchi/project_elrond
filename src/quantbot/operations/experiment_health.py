"""Read-only optional experiment measurements; no authority or external delivery."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypedDict

from quantbot.learner.experiment_contract import ExperimentManifest, WorkerReceipt
from quantbot.learner.experiment_coordinator import TickReceipt
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.operations.alerts import Condition


class ExperimentHealth(TypedDict):
    coordinator: str
    worker: str
    conditions: tuple[Condition, ...]


def measure_experiment_health(
    path: Path, *, expected_commit: str, now: datetime
) -> ExperimentHealth:
    if now.tzinfo is None or re.fullmatch("[a-f0-9]{40}", expected_commit) is None:
        raise ValueError("EXACT_EXPERIMENT_MONITOR_PIN_AND_CLOCK_REQUIRED")
    coordinator, worker = "UNVERIFIED", "UNVERIFIED"
    try:
        store = ExperimentStore(path, read_only=True)
        with store._transaction(write=False) as db:
            ids = tuple(row[0] for row in db.execute("SELECT experiment_id FROM states"))
        if len(ids) != 1:
            raise ValueError("SINGLE_CONFIGURED_EXPERIMENT_REQUIRED")
        events = store.read_as_of(ids[0], now)
        manifests = [e.decode(ExperimentManifest) for e in events if e.kind == "ExperimentManifest"]
        if len(manifests) != 1:
            raise ValueError("AUTHORITATIVE_MANIFEST_REQUIRED")
        frozen = manifests[0]
        ticks = [e.decode(TickReceipt) for e in events if e.kind == "TickReceipt"]
        if ticks:
            latest = max(ticks, key=lambda tick: tick.recorded_at)
            if (
                store.state(frozen.experiment_id).state != "FAULTED"
                and latest.clock_verified
                and latest.status not in {"FAULTED", "CLOCK_SYNC_REQUIRED"}
                and latest.synthetic == frozen.synthetic
                and latest.recorded_at <= now <= latest.next_wakeup_at + timedelta(minutes=2)
            ):
                coordinator = "FRESH"
        receipt = path.parent / "worker-receipt.json"
        if receipt.is_symlink() or receipt.stat().st_size > 8192:
            raise ValueError("WORKER_RECEIPT_UNVERIFIED")
        measured = WorkerReceipt.model_validate(json.loads(receipt.read_text(encoding="utf-8")))
        age = (now - measured.recorded_at).total_seconds()
        if (
            measured.experiment_id == frozen.experiment_id
            and measured.code_commit == expected_commit
            and measured.synthetic == frozen.synthetic
            and age >= 0
        ):
            if measured.status == "STARTED" and age <= 1020:
                worker = "RUNNING_BOUNDED"
            elif measured.status in {"NORMAL_WAIT", "SUCCEEDED"} and age <= 240:
                worker = "FRESH"
    except (OSError, ValueError, sqlite3.Error):
        # An unreadable worker cannot erase a separately verified coordinator receipt.
        pass
    conditions = tuple(
        Condition(
            "EXPERIMENT_" + component.upper() + "_UNVERIFIED",
            "research",
            "CRITICAL",
            "UNVERIFIED_COMPONENT_HEALTH",
            "DEVELOPMENT_OPERATOR",
            "Inspect the pinned shadow component receipt and scheduler; retain PAPER protection. "
            "Calendar waiting and statistical insufficiency do not establish component liveness.",
        )
        for component, status in (("coordinator", coordinator), ("worker", worker))
        if status == "UNVERIFIED"
    )
    return {"coordinator": coordinator, "worker": worker, "conditions": conditions}
