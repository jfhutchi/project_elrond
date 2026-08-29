"""Fail-closed QuantBot command-line surface."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
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
    # Reports only. Naming a source cannot spend it, and there is deliberately no flag here
    # that reserves or consumes anything: this command exists to be run before deciding to
    # spend, and a command that spent while answering would defeat its own purpose.
    capacity = commands.add_parser("research-capacity")
    capacity.add_argument("--as-of", dest="as_of")
    sweep = commands.add_parser("integrity-sweep")
    # Acts by default, unlike `register-hypothesis`. The polarity is deliberate: this
    # command *reduces* trust, and a safety control that does nothing unless someone
    # remembers a flag is a safety control that does nothing. Demotion is reversible and
    # preserves the forward evidence, so acting wrongly is cheap and not acting is not.
    sweep.add_argument("--dry-run", dest="dry_run", action="store_true")
    # The command every reproducible bundle already names in `reproduction_command`. Until
    # this existed the manifest told a reader to run something no CLI implemented.
    reproduce = commands.add_parser("reproduce-experiment")
    reproduce.add_argument("--experiment", required=True)
    reproduce.add_argument("--manifests", default="reports/manifests")
    # Where a preserved working-tree patch lives, when the original ran dirty. Optional, and its
    # absence is what makes a dirty run unreproducible rather than merely noted.
    reproduce.add_argument("--patch")
    # The command `MigrationRequiredError` tells the operator to run (#66). Opening a store no
    # longer upgrades it, so there has to be somewhere the authority deliberately lives.
    migrate = commands.add_parser("migrate-database")
    migrate.add_argument(
        "--database",
        help="store to upgrade; defaults to QUANTBOT_DB_PATH, the same one everything else reads",
    )
    # Refusing by default is the point: an upgrade is not reversible and the operator should have
    # seen what it would do before it does it.
    migrate.add_argument("--confirm", action="store_true")
    # The other half of the same boundary (#86). Opening a store no longer creates one, so
    # first-run bootstrap needs a name of its own rather than happening as a side effect of a
    # status command run against a mistyped path.
    create = commands.add_parser("create-database")
    create.add_argument(
        "--database",
        help="store to create; defaults to QUANTBOT_DB_PATH, the same one everything else reads",
    )
    create.add_argument("--confirm", action="store_true")
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


def _create_database(args: argparse.Namespace, stream: TextIO) -> int:
    """Bootstrap a new store, deliberately (#86).

    Refuses an existing store outright, and refuses to create one without `--confirm`. The
    second refusal is the useful one: the reason this command exists is that creating a store
    at the wrong path used to be indistinguishable from opening the right one, so a bootstrap
    that happened because somebody typed a path wrong would be no better here.
    """
    from quantbot.storage import Database
    from quantbot.storage.schema import SCHEMA_VERSION

    path = Path(args.database or os.environ.get("QUANTBOT_DB_PATH", "quantbot.db"))
    payload: dict[str, object] = {
        "ok": False,
        "command": "create-database",
        "database": str(path.resolve()),
        "target_schema_version": SCHEMA_VERSION,
        "created": False,
    }

    if path.exists() and path.stat().st_size > 0:
        payload["error"] = f"{path} already holds a database; nothing to create"
        json.dump(payload, stream, sort_keys=True)
        return 2
    if not args.confirm:
        # The resolved path is echoed rather than the argument, because the mistake this guards
        # against is usually a relative path resolving somewhere unexpected.
        payload["error"] = (
            f"would create a new store at {path.resolve()} with an engaged kill switch. Check "
            "that path is where you mean, then re-run with --confirm"
        )
        json.dump(payload, stream, sort_keys=True)
        return 2

    Database.create(path).close()
    payload["ok"] = True
    payload["created"] = True
    json.dump(payload, stream, sort_keys=True)
    return 0


def _migrate_database(args: argparse.Namespace, stream: TextIO) -> int:
    """Upgrade a store to head, deliberately (#66).

    Reports what it would do and stops unless `--confirm` is given. A migration is not
    reversible, and the incident this exists to prevent happened precisely because a schema
    upgrade could occur without anyone deciding to perform one.
    """
    from quantbot.storage import Database, MigrationRequiredError, UnsupportedSchemaVersionError
    from quantbot.storage.schema import SCHEMA_VERSION

    path = Path(args.database or os.environ.get("QUANTBOT_DB_PATH", "quantbot.db"))
    payload: dict[str, object] = {
        "ok": False,
        "command": "migrate-database",
        "database": str(path),
        "target_schema_version": SCHEMA_VERSION,
        "migrated": False,
    }

    if not path.exists() or path.stat().st_size == 0:
        payload["error"] = f"{path} holds no database to migrate"
        json.dump(payload, stream, sort_keys=True)
        return 2

    try:
        # An open that succeeds means there is nothing to do -- and it cannot have migrated
        # anything, which is the property #66 establishes.
        Database(path).close()
    except MigrationRequiredError as behind:
        payload["current_schema_version"] = behind.actual
        if not args.confirm:
            payload["error"] = (
                f"{path} is at schema version {behind.actual} and would be upgraded to "
                f"{SCHEMA_VERSION}. Take a backup, then re-run with --confirm"
            )
            json.dump(payload, stream, sort_keys=True)
            return 2
        Database.migrate(path).close()
        payload.update(ok=True, migrated=True)
        json.dump(payload, stream, sort_keys=True)
        return 0
    except UnsupportedSchemaVersionError as ahead:
        # Ahead of this build. There is no downgrade here on purpose: rewriting a store to suit
        # an older checkout is the destructive direction, and it is the operator's call.
        payload["current_schema_version"] = ahead.actual
        payload["error"] = (
            f"{path} is at schema version {ahead.actual}, ahead of the {SCHEMA_VERSION} this "
            "build supports. It was written by a newer checkout; reconcile the code rather than "
            "the store"
        )
        json.dump(payload, stream, sort_keys=True)
        return 2

    payload.update(ok=True, current_schema_version=SCHEMA_VERSION)
    payload["note"] = "already at head; nothing to migrate"
    json.dump(payload, stream, sort_keys=True)
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    context: CLIContext | None = None,
    output: TextIO | None = None,
) -> int:
    stream = output or sys.stdout
    parser = build_parser(output=stream)
    args = parser.parse_args(argv)

    if args.command == "migrate-database":
        # Handled before a context exists, because building one opens a database -- and the store
        # this command is for is precisely the one that cannot be opened. A migration command
        # that needed a successful open would be unreachable exactly when it was needed.
        return _migrate_database(args, stream)
    if args.command == "create-database":
        # Same reason, more so: there is nothing to open yet.
        return _create_database(args, stream)

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
            from quantbot.research.milestone import assess_milestone

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
                milestone = assess_milestone(session, as_of=datetime.now(UTC).date())

            result = {
                "ok": True,
                "models": models,
                "blocked_by": blocked_by,
                # #52: the two numbers side by side, because only one of them was ever
                # reported. Task counts and model wiring move when engineering happens;
                # `profitability` moves only when evidence about the market changes, and a
                # long list of closed architecture issues otherwise reads as progress toward
                # an edge.
                "profitability": {
                    "milestone": (
                        "BELOW_M0" if milestone.current is None else milestone.current.value
                    ),
                    "stuck_on": (
                        None
                        if milestone.next_check is None
                        else milestone.next_check.explain()
                    ),
                    "candidate_edges": list(milestone.candidate_edges),
                    "forward_supported_edges": list(milestone.forward_supported),
                    "clean_confirmatory_capacity": list(milestone.clean_capacity),
                    # A legitimate destination, not a failure. Naming it is what stops the
                    # alternative: adding another framework because nothing else can be said.
                    "no_edge_established": milestone.no_edge_established,
                    "levels": [
                        {
                            "level": check.level.value,
                            "reached": check.reached,
                            "evidence": list(check.evidence),
                            "blocking": list(check.blocking),
                            "unassessed": list(check.unassessed),
                        }
                        for check in milestone.checks
                    ],
                },
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
        elif args.command == "research-capacity":
            # Function-scope import for the same reason as the other research commands.
            from quantbot.research.capacity import capacity_inventory

            as_of = (
                date.fromisoformat(args.as_of)
                if getattr(args, "as_of", None)
                else datetime.now(UTC).date()
            )
            with active.database.transaction() as session:
                inventory = capacity_inventory(session, as_of=as_of)
            result = {
                "ok": True,
                "as_of": as_of.isoformat(),
                "sources": [
                    {
                        "observation_identity": row.observation_identity,
                        "datasets": list(row.aliases),
                        "asset_class": row.source.asset_class.value,
                        "capability": row.source.capability.value,
                        "status": row.status.value,
                        "usable_for_confirmation": row.usable_for_confirmation,
                        "trials_spent": row.trials,
                        "consumers": list(row.consumers),
                        "by_family": dict(row.by_family),
                        "reserved_by": list(row.reserved_by),
                        # A count of real sessions, not a date implying an open-ended stream.
                        # The first version reported "unsearched from <calendar day>", which on
                        # a Friday-consumed window named a Saturday (#50).
                        "unsearched_sessions": len(row.unsearched_sessions),
                        "unsearched_from": (
                            None
                            if row.unconsumed_from is None
                            else row.unconsumed_from.isoformat()
                        ),
                        "point_in_time": row.source.point_in_time,
                        "answers": row.source.answers,
                    }
                    for row in inventory
                ],
                # Named rather than left to be inferred. #50 exists because "the data platform
                # is finished" was read as "there is fresh statistical budget", and the two
                # facts are unrelated.
                "note": (
                    "engineering capability is not statistical capacity; a source reporting "
                    "EXHAUSTED stays exhausted when another provider serves the same dates"
                ),
            }
        elif args.command == "research-cycle":
            # Function-scope import for the same reason as the other research commands.
            from quantbot.research.cycle import actionable, drain
            from quantbot.research.director import ResearchDirector, TaskState
            from quantbot.research.memory import ResearchMemory
            from quantbot.research.prior import load_prior_findings
            from quantbot.research.stages import capacity_stage, novelty_stage

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
                # Capacity first, then novelty. Both are cheap and both can stop a question
                # before anything expensive runs, but a question nothing could confirm should
                # not consume a memory query either (#50).
                stages = {
                    TaskState.PROPOSED: capacity_stage(
                        session, novelty_stage(ResearchMemory(session))
                    )
                }
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
        elif args.command == "reproduce-experiment":
            # Function-scope import for the same reason as the other research commands.
            from quantbot.research.reproduce import (
                ManifestNotFound,
                assess_reproducibility,
                find_manifest,
            )
            from quantbot.research.reproducibility import capture_environment

            try:
                manifest_path, manifest = find_manifest(
                    Path(args.manifests), args.experiment
                )
            except ManifestNotFound as absent:
                result = {"ok": False, "error": str(absent), "reproduction_performed": False}
            else:
                assessment = assess_reproducibility(
                    manifest_path,
                    manifest,
                    current_lock_hash=capture_environment(host_class="cli").dependency_lock_hash,
                    preserved_patch=Path(args.patch) if args.patch else None,
                )
                result = {
                    # `ok` is about whether the inputs are recoverable. It is deliberately not
                    # named `reproduced`: nothing ran.
                    "ok": assessment.can_attempt,
                    "experiment_id": assessment.experiment_id,
                    "manifest": str(assessment.manifest_path),
                    "manifest_hash": assessment.manifest_hash,
                    # A field rather than an omission. A report that merely never mentions
                    # running the experiment reads, to a hurried reader, exactly like one that
                    # ran it and found no differences.
                    "reproduction_performed": assessment.performed,
                    "inputs": [
                        {
                            "name": check.name,
                            "present": check.present,
                            "intact": check.intact,
                        }
                        for check in assessment.inputs
                    ],
                    "blocking": list(assessment.blocking),
                    "drift": list(assessment.drift),
                    "outstanding_capabilities": list(assessment.outstanding),
                    "note": (
                        "this reports whether a rerun could be attempted and does not attempt "
                        "one; #48 remains open"
                    ),
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
