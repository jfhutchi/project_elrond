"""Portable read-only recovery capture/comparison; Python 3.11+, standard library only.

Run with python -I -S -B to exclude installed packages and prevent bytecode writes.
Load only audited leaf modules by file path, never quantbot's operational package initializer.
Output goes to stdout; only the operator's redirected report files are written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import runpy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def leaf(relative: str) -> dict[str, Any]:
    return runpy.run_path(str(ROOT / "src/quantbot" / relative))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--repo", type=Path, default=Path.cwd())
    capture.add_argument("--candidate", action="append", default=[])
    capture.add_argument("--scan-root", action="append", default=[])
    capture.add_argument("--intended-commit")
    compare = commands.add_parser("compare")
    compare.add_argument("--report", action="append", required=True, help="label=path.json")
    compare.add_argument("--expect", action="append", default=[])
    compare.add_argument("--format", choices=("json", "markdown"), default="markdown")
    args = parser.parse_args()
    store_tools = leaf("operations/store_inventory.py")
    if args.command == "compare":
        try:
            reports = {}
            for item in args.report:
                label, filename = item.split("=", 1)
                if (
                    not label
                    or label in reports
                    or Path(filename).stat().st_size > 32 * 1024 * 1024
                ):
                    raise ValueError("INVALID_OR_OVERSIZED_REPORT")
                report = json.loads(Path(filename).read_text(encoding="utf-8-sig"))
                if not isinstance(report, dict):
                    raise ValueError("INVALID_REPORT")
                reports[label] = report
            comparison = leaf("operations/recovery_compare.py")
            result = store_tools["redact"](
                comparison["compare_reports"](reports, expected_hosts=args.expect), os.environ
            )
            if args.format == "markdown":
                print(comparison["render_comparison"](result), end="")
            else:
                print(json.dumps(result, sort_keys=True, indent=2))
            return 1  # Evidence never grants recovery/startup authority.
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "INVALID_OR_UNREADABLE_REPORT",
                        "store_authority": "UNKNOWN",
                        "startup_allowed": False,
                    }
                )
            )
            return 2

    checkout = leaf("operations/checkout.py")
    target = leaf("storage/target.py")
    paths = set()
    errors = []
    configured = "UNKNOWN"
    try:
        resolved = target["resolve_database_target"](os.environ)
        resolved.require_enabled()
        configured = str(resolved.path.resolve())
        paths.add(resolved.path.resolve())
    except target["DatabaseTargetError"] as error:
        errors.append(error.code)
    scans = [
        store_tools["discover_stores"](Path(root).expanduser().resolve()) for root in args.scan_root
    ]
    for candidate in [*args.candidate, *(p for scan in scans for p in scan["candidates"])]:
        try:
            resolved = target["coerce_target"](candidate)
            resolved.require_enabled()
            paths.add(resolved.path.resolve())
        except target["DatabaseTargetError"] as error:
            errors.append(error.code)
    stores = [store_tools["inspect_store"](path) for path in sorted(paths)]
    plausible = [store for store in stores if store_tools["plausible_store"](store)]
    source_files = [
        Path(__file__).resolve(),
        *(
            ROOT / "src/quantbot" / relative
            for relative in (
                "operations/store_inventory.py",
                "operations/checkout.py",
                "operations/host_inventory.py",
                "operations/recovery_compare.py",
                "storage/target.py",
            )
        ),
    ]
    result = {
        "report_version": 1,
        "ok": False,
        "command": "recovery-capture",
        "as_of": datetime.now(UTC).isoformat(),
        "host": {
            "hostname": platform.node(),
            "os": platform.system(),
            "architecture": platform.machine(),
            "version": platform.version(),
        },
        "collector": {
            "python": platform.python_version(),
            "source_sha256": {
                path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in source_files
            },
        },
        "checkout": checkout["checkout_evidence"](args.repo.resolve(), args.intended_commit),
        "working_directory": str(Path.cwd()),
        "configured_database": configured,
        "process_environment_names": sorted(
            name
            for name in os.environ
            if name.startswith(("QUANTBOT_", "ALPACA_"))
            or name
            in (
                "TRADING_MODE",
                "BROKER_ENVIRONMENT",
                "LIVE_TRADING_ACKNOWLEDGED",
                "EXPECTED_ACCOUNT_ID",
                "KILL_SWITCH",
            )
        ),
        "configured_mode": os.environ.get("TRADING_MODE", "UNKNOWN"),
        "configured_broker_environment": os.environ.get("BROKER_ENVIRONMENT", "UNKNOWN"),
        "candidate_errors": errors,
        "candidate_scans": scans,
        "stores": stores,
        "os_inventory": leaf("operations/host_inventory.py")["host_inventory"](),
        "store_authority": "AMBIGUOUS"
        if len(
            {
                (
                    store["file_id"]
                    if store.get("file_id") not in (None, "UNKNOWN")
                    else store["path"]
                )
                for store in plausible
            }
        )
        > 1
        else "UNKNOWN",
        "pi_state": "UNKNOWN",
        "coordinator_authority": "UNKNOWN",
        "process_id": os.getpid(),
        "process_role": "TOOL",
        "startup_preflight": {
            "allowed": False,
            "blockers": [
                "GLOBAL_AUTHORITY_UNVERIFIED",
                "OTHER_HOST_RETIREMENT_UNVERIFIED",
                "BROKER_RECONCILIATION_NOT_PROBED",
            ],
        },
        "note": "Bounded read-only snapshot, not absence/quiescence/authority proof. "
        "Raw timestamps/rows are stored claims. No computed freshness in portable mode. "
        "Missing tables/columns are null, bounded rows are not complete history. "
        "No broker call, runtime import, migration, coordinator start or kill-switch change.",
    }
    print(json.dumps(store_tools["redact"](result, os.environ), sort_keys=True, indent=2))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
