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


class _DryRun(Exception):
    """Abandons the transaction after the gates have run, so a preview costs no budget.

    Registration is not reversible in the way that matters: it reserves protected evaluation
    windows and permanently raises the multiple-testing burden for everything registered after
    it. Rolling back rather than skipping the write means the preview is produced by the real
    gates against real durable state -- the cumulative trial count is read from the database,
    not assumed -- while spending none of it.
    """

    def __init__(self, summary: dict[str, object]) -> None:
        self.summary = summary
        super().__init__("dry run")


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
    for name in ("status", "doctor", "sync-data", "reconcile", "run-once", "daemon"):
        commands.add_parser(name)
    backtest = commands.add_parser("backtest")
    backtest.add_argument("--variant")
    backtest.add_argument("--initial-cash", dest="initial_cash", default="100000")
    weekly = commands.add_parser("report-weekly")
    weekly.add_argument("--iso-year", dest="iso_year", type=int)
    weekly.add_argument("--iso-week", dest="iso_week", type=int)
    kill = commands.add_parser("kill-switch")
    kill_actions = kill.add_subparsers(dest="kill_action", required=True)
    for action in ("engage", "clear"):
        action_parser = kill_actions.add_parser(action)
        action_parser.add_argument("--reason", required=True)
    registered = commands.add_parser("hypotheses")
    registered.add_argument("--family")
    freeze = commands.add_parser("register-hypothesis")
    freeze.add_argument("--draft", required=True)
    freeze.add_argument("--critique", required=True)
    # Registration is not reversible in the way that matters: it reserves protected windows and
    # permanently raises the multiple-testing burden for everything registered after it. So the
    # default is to report what the gates say and persist nothing.
    freeze.add_argument("--commit", action="store_true")
    smoke = commands.add_parser("paper-smoke")
    smoke.add_argument("--acknowledgement", required=True)
    return parser


def _default_context() -> CLIContext:
    """Build the wired runtime, degrading to a safe read-only context without credentials."""
    from quantbot.runtime import RuntimeConfigurationError, build_cli_context

    try:
        return build_cli_context()
    except RuntimeConfigurationError as error:
        reason = str(error)

        def unavailable(command: str) -> CommandHandler:
            return lambda _args: {"ok": False, "command": command, "reason": reason}

        database_path = Path(os.environ.get("QUANTBOT_DB_PATH", "quantbot.db"))
        return CLIContext(
            settings=Settings(),
            database=Database(database_path),
            handlers={
                command: unavailable(command)
                for command in (
                    "doctor",
                    "sync-data",
                    "reconcile",
                    "run-once",
                    "daemon",
                    "backtest",
                    "report-weekly",
                    "paper-smoke",
                )
            },
        )


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
        elif args.command == "hypotheses":
            # Imported here, not at module scope. The kill switch lives in this file, and it
            # must not stop working because research code failed to import.
            from quantbot.research import HypothesisRegistry, summarize

            with active.database.transaction() as session:
                registry = HypothesisRegistry(session)
                registrations = registry.list_registrations(family_id=args.family)
            result = {
                "ok": True,
                "registered": [summarize(entry) for entry in registrations],
                "note": (
                    "only a listed registration may back a CONFIRMATORY experiment; "
                    "any other analysis is EXPLORATORY and is not evidence"
                ),
            }
        elif args.command == "register-hypothesis":
            # Function-scope import for the same reason as `hypotheses` above: the kill switch
            # lives in this file and must not stop working because research code failed to
            # import.
            from quantbot.research import (
                Critique,
                HypothesisDraft,
                HypothesisRegistry,
                RegistrationRefused,
                summarize,
            )

            draft = HypothesisDraft.model_validate_json(
                Path(args.draft).read_text(encoding="utf-8")
            )
            critique = Critique.model_validate_json(Path(args.critique).read_text(encoding="utf-8"))
            now = datetime.now(UTC)
            try:
                with active.database.transaction() as session:
                    registration = HypothesisRegistry(session).register(
                        draft, now=now, critique=critique
                    )
                    frozen = summarize(registration)
                    if not args.commit:
                        # Every gate ran against real durable state -- the cumulative trial
                        # burden is read from the database, not assumed -- and the transaction
                        # is then abandoned, so the verdict is real and the spend is not.
                        raise _DryRun(frozen)
            except _DryRun as preview:
                result = {
                    "ok": True,
                    "committed": False,
                    "registration": preview.summary,
                    "note": (
                        "gates cleared against live state and nothing was persisted; "
                        "re-run with --commit to reserve the windows and spend the trial"
                    ),
                }
            except RegistrationRefused as refusal:
                result = {
                    "ok": False,
                    "committed": False,
                    "reason": refusal.reason.value,
                    "detail": refusal.detail,
                }
            else:
                result = {"ok": True, "committed": True, "registration": frozen}
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
