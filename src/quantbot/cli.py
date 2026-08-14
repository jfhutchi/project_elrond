"""Fail-closed QuantBot command-line surface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from quantbot.config import Settings
from quantbot.domain import ReconciliationStatus
from quantbot.operations import (
    KillSwitchController,
    ReadinessEvidence,
    authorize_paper_smoke,
)
from quantbot.storage import Database, StorageRepository

CommandHandler = Callable[[argparse.Namespace], Mapping[str, object]]


@dataclass(frozen=True, slots=True)
class CLIContext:
    settings: Settings
    database: Database
    handlers: Mapping[str, CommandHandler]
    reported_account_id: str | None = None
    clearance_evidence: ReadinessEvidence | None = None


def build_parser(*, output: TextIO | None = None) -> argparse.ArgumentParser:
    _ = output
    parser = argparse.ArgumentParser(prog="quantbot")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "status",
        "doctor",
        "sync-data",
        "reconcile",
        "run-once",
        "daemon",
        "backtest",
        "report-weekly",
    ):
        commands.add_parser(name)
    kill = commands.add_parser("kill-switch")
    kill_actions = kill.add_subparsers(dest="kill_action", required=True)
    for action in ("engage", "clear"):
        action_parser = kill_actions.add_parser(action)
        action_parser.add_argument("--reason", required=True)
    smoke = commands.add_parser("paper-smoke")
    smoke.add_argument("--acknowledgement", required=True)
    return parser


def _default_context() -> CLIContext:
    settings = Settings()
    database_path = Path(os.environ.get("QUANTBOT_DB_PATH", "quantbot.db"))
    return CLIContext(settings=settings, database=Database(database_path), handlers={})


def _unconfigured(command: str) -> dict[str, object]:
    return {
        "ok": False,
        "command": command,
        "reason": "OPERATION_HANDLER_NOT_CONFIGURED",
    }


def _durable_reconciliation_successful(database: Database) -> bool:
    with database.transaction() as session:
        latest = StorageRepository(session).get_latest_reconciliation()
    return (
        latest is not None and latest.status is ReconciliationStatus.RECONCILED and not latest.diffs
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    context: CLIContext | None = None,
    output: TextIO | None = None,
) -> int:
    stream = output or sys.stdout
    parser = build_parser(output=stream)
    args = parser.parse_args(argv)
    owned_context = context is None
    active = context or _default_context()
    try:
        controller = KillSwitchController(active.database)
        if args.command == "status":
            state = controller.get()
            result: Mapping[str, object] = {
                "ok": True,
                "settings": active.settings.safe_summary(),
                "kill_switch": {
                    "engaged": state.engaged,
                    "reason": state.reason,
                    "updated_at": state.updated_at.isoformat(),
                },
            }
        elif args.command == "kill-switch":
            now = datetime.now(UTC)
            if args.kill_action == "engage":
                state = controller.engage(reason=args.reason, updated_at=now)
            else:
                evidence = active.clearance_evidence or ReadinessEvidence(
                    paper_mode=False,
                    account_verified=False,
                    broker_healthy=False,
                    data_healthy=False,
                    risk_healthy=False,
                    reconciliation_successful=False,
                )
                state = controller.clear(
                    reason=args.reason,
                    evidence=evidence,
                    updated_at=now,
                )
            result = {"ok": True, "engaged": state.engaged, "reason": state.reason}
        elif args.command == "paper-smoke":
            if active.reported_account_id is None:
                raise ValueError("paper smoke requires a broker-reported account ID")
            readiness = active.clearance_evidence or ReadinessEvidence(
                paper_mode=False,
                account_verified=False,
                broker_healthy=False,
                data_healthy=False,
                risk_healthy=False,
                reconciliation_successful=False,
            )
            authorize_paper_smoke(
                active.settings,
                reported_account_id=active.reported_account_id,
                durable_kill_switch_engaged=controller.get().engaged,
                durable_reconciliation_successful=_durable_reconciliation_successful(
                    active.database
                ),
                readiness_evidence=readiness,
                acknowledgement=args.acknowledgement,
            )
            handler = active.handlers.get("paper-smoke")
            result = handler(args) if handler is not None else _unconfigured("paper-smoke")
        else:
            handler = active.handlers.get(args.command)
            result = handler(args) if handler is not None else _unconfigured(args.command)
        stream.write(json.dumps(result, ensure_ascii=True, sort_keys=True) + "\n")
        return 0 if bool(result.get("ok")) else 2
    finally:
        if owned_context:
            active.database.close()


if __name__ == "__main__":
    raise SystemExit(main())
