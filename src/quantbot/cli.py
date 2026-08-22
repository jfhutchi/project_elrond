"""Fail-closed QuantBot command-line surface."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from quantbot.config import Settings
from quantbot.domain import ReconciliationStatus
from quantbot.operations import (
    KillSwitchController,
    MeasuredReadiness,
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
    #: Probes the world and returns measured readiness. `None` when this context was built
    #: without a broker, in which case clearing is refused rather than falling back to a
    #: declared evidence object -- which is the defect `readiness.py` was written to remove.
    measure: Callable[[], Coroutine[Any, Any, MeasuredReadiness]] | None = None


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
    commands.add_parser("research-status")
    sweep = commands.add_parser("integrity-sweep")
    # Acts by default, unlike `register-hypothesis`. The polarity is deliberate: this
    # command *reduces* trust, and a safety control that does nothing unless someone
    # remembers a flag is a safety control that does nothing. Demotion is reversible and
    # preserves the forward evidence, so acting wrongly is cheap and not acting is not.
    sweep.add_argument("--dry-run", dest="dry_run", action="store_true")
    verify = commands.add_parser("verify-manifest")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--against")
    cycle = commands.add_parser("research-cycle")
    # Required, with no default, for the same reason `drain` requires it: each stage a run
    # advances can permanently raise the luck bar, so how many is a decision the operator
    # makes explicitly rather than inherits.
    cycle.add_argument("--max-steps", dest="max_steps", type=int, required=True)
    # Reports by default. Advancing writes durable task events, and the handler set will
    # grow to include stages that spend budget -- so the safe polarity is the one that
    # stays safe as more handlers arrive.
    cycle.add_argument("--commit", action="store_true")
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
                if active.measure is None:
                    # No fallback to a declared evidence object. A context without a broker
                    # cannot establish that it is safe to resume, and saying so is the honest
                    # answer; manufacturing all-False evidence would produce the same refusal
                    # for a reason that reads like a measurement.
                    raise RuntimeError(
                        "this context cannot measure readiness, so it cannot clear the kill "
                        "switch; run where the broker is configured"
                    )
                measured = asyncio.run(active.measure())
                state = controller.clear(
                    reason=args.reason,
                    measured=measured,
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
        elif args.command == "research-status":
            # Function-scope import, like the other research commands: the kill switch lives in
            # this file and must not stop working because research code failed to import.
            from quantbot.research.composition import (
                ResearchNotConfigured,
                model_configuration,
            )
            from quantbot.research.cycle import actionable
            from quantbot.research.director import ResearchDirector

            try:
                configuration = model_configuration()
                models = {
                    "endpoint": configuration.endpoint,
                    "generator": configuration.generator,
                    "critic": configuration.critic,
                    "api_key_configured": configuration.api_key is not None,
                }
                blocked_by = None
            except ResearchNotConfigured as unconfigured:
                # Reported, not raised. "Research cannot run yet" is a state an operator needs
                # to see alongside everything else, not an error that hides the rest of it.
                models = None
                blocked_by = str(unconfigured)

            with active.database.transaction() as session:
                director = ResearchDirector(session)
                tasks = director.by_state()
                # No stage handlers are passed, so nothing is actionable: this reports the
                # queue, and deliberately does not advance it. A status command that moved
                # research forward would be the worst possible place to spend a trial.
                ready = len(actionable(director, {}))

            result = {
                "ok": True,
                "models": models,
                "blocked_by": blocked_by,
                "tasks": {
                    "total": len(tasks),
                    "by_state": {
                        state: sum(1 for task in tasks if task.state.value == state)
                        for state in sorted({task.state.value for task in tasks})
                    },
                },
                "actionable_now": ready,
                "note": ("research-status reports and never advances; use the cycle for that"),
            }
        elif args.command == "research-cycle":
            # Function-scope import for the same reason as the other research commands.
            from quantbot.research.cycle import actionable, drain
            from quantbot.research.director import ResearchDirector, TaskState
            from quantbot.research.memory import ResearchMemory
            from quantbot.research.prior import load_prior_findings
            from quantbot.research.stages import novelty_stage

            with active.database.transaction() as session:
                # Load what the project already knows before deciding anything. The novelty gate
                # reads memory, and on 2026-08-22 the live store was empty while REFUTED.md held
                # twenty-four settled findings -- so the gate passed questions this project had
                # already answered. Idempotent, so running it every cycle costs one query.
                seeded = load_prior_findings(ResearchMemory(session))
                director = ResearchDirector(session)
                # The stages that can be decided mechanically. Scouting, critique and experiment
                # design need a model or a measurement; `actionable()` treats a state with no
                # handler as not-actionable rather than as an error, so those tasks stay
                # visibly stuck instead of being advanced by something that did not run them.
                stages = {TaskState.PROPOSED: novelty_stage(ResearchMemory(session))}
                runnable = actionable(director, stages)

                if not args.commit:
                    result = {
                        "ok": True,
                        "committed": False,
                        "prior_findings_loaded": seeded,
                        "runnable_now": [task.task_id for task, _ in runnable],
                        "stages_available": sorted(state.value for state in stages),
                        "note": "nothing advanced; re-run with --commit to act",
                    }
                else:
                    outcomes = drain(
                        director,
                        stages,
                        actor="research-cycle",
                        now=datetime.now(UTC),
                        max_steps=args.max_steps,
                    )
                    result = {
                        "ok": True,
                        "committed": True,
                        "advanced": [
                            {
                                "task_id": None if outcome.task is None else outcome.task.task_id,
                                "moved_to": (
                                    None if outcome.moved_to is None else outcome.moved_to.value
                                ),
                                "reason": outcome.reason,
                            }
                            # An idle outcome carries no task and is dropped: "the queue had
                            # nothing to do" is reported by the empty list, not by a row.
                            for outcome in outcomes
                            if not outcome.idle
                        ],
                        # A drain that stopped early is not the same as one with nothing left,
                        # and reporting only the count would make them look alike.
                        "steps_requested": args.max_steps,
                    }
        elif args.command == "verify-manifest":
            # Function-scope import for the same reason as the other research commands.
            from quantbot.research.manifest import ExperimentManifest
            from quantbot.research.reproducibility import (
                capture_git_state,
                check_invariants,
                compare,
            )

            manifest = ExperimentManifest.model_validate_json(
                Path(args.manifest).read_text(encoding="utf-8")
            )
            report = check_invariants(manifest)
            commit, dirty = capture_git_state()
            differences = []
            if args.against is not None:
                other = ExperimentManifest.model_validate_json(
                    Path(args.against).read_text(encoding="utf-8")
                )
                differences = [
                    {"field": item.field, "recorded": item.left, "other": item.right,
                     "material": item.material}
                    for item in compare(manifest, other)
                ]

            result = {
                # `ok` is about the invariants only. Code drift is reported and never folded in:
                # a result computed on a different commit is not thereby wrong, and marking it
                # failed would train a reader to ignore this field.
                "ok": report.ok,
                "manifest_hash": manifest.manifest_hash,
                "experiment_id": manifest.experiment_id,
                "mode": manifest.mode,
                "violations": [
                    {"invariant": item.invariant, "detail": item.detail}
                    for item in report.violations
                ],
                "checked": list(report.checked),
                # Surfaced, never summarised away. A check that quietly did nothing because the
                # result lacked the figure it needs looks exactly like a check that passed.
                "skipped": list(report.skipped),
                # `code` is optional on the model, and a bundle without it cannot have its
                # drift checked. Reported as unrecorded rather than defaulted to the current
                # commit, which would claim the run happened on code nobody showed it did.
                "code": {
                    "recorded_commit": (
                        None if manifest.code is None else manifest.code.git_commit
                    ),
                    "current_commit": commit,
                    "current_tree_dirty": dirty,
                    "same_commit": (
                        None if manifest.code is None else manifest.code.git_commit == commit
                    ),
                },
                "differences": differences,
            }
        elif args.command == "integrity-sweep":
            # Function-scope import for the same reason as the other research commands.
            from quantbot.research.promotion import (
                NON_INFORMATIONAL,
                demote_on_integrity_incidents,
            )
            from quantbot.storage import StorageRepository

            with active.database.transaction() as session:
                if args.dry_run:
                    repository = StorageRepository(session)
                    open_incidents = [
                        (strategy, incident)
                        for strategy, incident, severity in (
                            repository.unresolved_incidents_with_strategy()
                        )
                        if severity.upper() in NON_INFORMATIONAL
                    ]
                    result = {
                        "ok": True,
                        "dry_run": True,
                        "would_consider": [
                            {"strategy_id": strategy, "incident_id": incident}
                            for strategy, incident in open_incidents
                        ],
                        "note": "nothing was demoted; re-run without --dry-run to act",
                    }
                else:
                    outcome = demote_on_integrity_incidents(
                        session, now=datetime.now(UTC), actor="cli"
                    )
                    result = {
                        "ok": True,
                        "dry_run": False,
                        "demoted": [
                            {"strategy_id": moved.strategy_id, "stage": moved.stage.value}
                            for moved in outcome.demoted
                        ],
                        # Surfaced rather than summarised away: a sweep that demoted nothing is
                        # not the same as a system with nothing wrong.
                        "unattributed_incidents": list(outcome.unattributed),
                        "clean": outcome.clean,
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
