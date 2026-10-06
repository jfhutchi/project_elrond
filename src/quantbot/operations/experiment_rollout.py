"""Prepare one no-order shadow task; backups never restore operational state."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quantbot.learner import experiment_launch as launch
from quantbot.learner.experiment_contract import ExperimentManifest
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.five_minute_launch import clock_healthy
from quantbot.research.manifest import content_id

TASK_NAME = "Elrond Adaptive Paper Shadow"


def prepare_shadow_rollout(
    config_path: Path, *, expected_commit: str, backup: Path, write_backup: bool = False
) -> dict[str, Any]:
    config = launch.ExperimentLaunchConfig.model_validate(launch._read_json(config_path))
    raw = config_path.read_bytes()
    if launch.ExperimentLaunchConfig.model_validate_json(raw) != config:
        raise ValueError("PINNED_CONFIGURATION_CHANGED")
    if config.code_commit != expected_commit:
        raise ValueError("SHADOW_ROLLOUT_PIN_MISMATCH")
    if config.research_sources is not None:
        raise ValueError("SEPARATE_CHARGED_RESEARCH_COMMISSIONING_REQUIRED")
    if config.environment_file is None or not config.environment_file.is_file():
        raise ValueError("EXISTING_CONFIGURED_ENVIRONMENT_REQUIRED")
    frozen = launch.load_manifest(config)
    launch.verify_checkout(config)
    if not clock_healthy(config.clock_receipt, datetime.now(UTC)):
        raise ValueError("CLOCK_SYNC_REQUIRED")
    if not backup.is_absolute() or backup.is_symlink() or backup.exists():
        raise ValueError("NEW_ABSOLUTE_BACKUP_REQUIRED")
    target = backup.resolve()
    sources: tuple[Path, ...] = (
        config_path,
        config.manifest_path,
        config.root,
        config.operations_database,
        config.declaration_database,
        config.clock_receipt,
    )
    if config.environment_file is not None:
        sources += (config.environment_file,)
    if any(
        target == p.resolve() or target in p.resolve().parents or p.resolve() in target.parents
        for p in sources
    ):
        raise ValueError("BACKUP_SOURCE_OVERLAP")
    manifest_raw = config.manifest_path.read_bytes()
    # Boundaries are rechecked against the bytes actually copied, not old parsed values.
    if (
        config_path.read_bytes() != raw
        or content_id(ExperimentManifest.model_validate_json(manifest_raw)) != config.manifest_hash
    ):
        raise ValueError("PINNED_CONFIGURATION_CHANGED")
    if config.store.exists():
        ExperimentStore(config.store, read_only=True)
    report: dict[str, Any] = {
        "version": 1,
        "task_name": TASK_NAME,
        "code_commit": expected_commit,
        "manifest_hash": content_id(frozen),
        "config_hash": hashlib.sha256(raw).hexdigest(),
        "mode": "SHADOW",
        "trading_enabled": False,
        "maximum_workers": 2,
        "protection_interruption": False,
        "operational_writes": False,
        "copied": [],
        "rollback": "STOP_ONLY_THIS_TASK_PRESERVE_ALL_NEW_EVIDENCE_NO_STATE_RESTORE",
    }
    if write_backup:
        backup.mkdir(parents=True, exist_ok=False)
        (backup / "config.json").write_bytes(raw)
        (backup / "manifest.json").write_bytes(manifest_raw)
        report["copied"] = ["config.json", "manifest.json"]
        if config.store.exists():
            with closing(
                sqlite3.connect(config.store.resolve().as_uri() + "?mode=ro", uri=True)
            ) as source:
                with closing(sqlite3.connect(backup / "experiment.db")) as destination:
                    source.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                        raise ValueError("EXPERIMENT_BACKUP_INTEGRITY_FAILED")
            report["copied"].append("experiment.db")
        (backup / "rollback.json").write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--write-backup", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(
            json.dumps(
                prepare_shadow_rollout(
                    args.config,
                    expected_commit=args.commit,
                    backup=args.backup,
                    write_backup=args.write_backup,
                ),
                sort_keys=True,
            )
        )
        return 0
    except (OSError, ValueError, sqlite3.Error):
        print("SHADOW_ROLLOUT_REFUSED; no task registration or operational-state restore performed")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
