"""Explicitly commissioned adaptive PAPER consumer in the coordinator process.

This launcher cannot create an approval, activation, deployment or promotion.
It never fits inline, rewrites evidence, disables another task or enables live
trading. The separate SHADOW launcher remains incapable of order construction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self
from xml.etree import ElementTree as ET

from pydantic import Field, model_validator

from quantbot.config import Settings
from quantbot.domain import StrategyIdentity
from quantbot.learner.estimators import predict
from quantbot.learner.experiment_activation import active_artifact
from quantbot.learner.experiment_contract import (
    CandidateArtifact,
    DecisionRecord,
    ExperimentManifest,
)
from quantbot.learner.experiment_coordinator import ActiveCycleContext
from quantbot.learner.experiment_launch import (
    ExperimentLaunchConfig,
    PreparedExecution,
    _read_json,
    load_manifest,
    verify_checkout,
)
from quantbot.learner.experiment_observation import ObservationBatch
from quantbot.learner.experiment_paper import build_pooled_paper_services
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.five_minute_launch import clock_healthy
from quantbot.learner.forward import _DECLARATIONS, _REGISTRY, _guard
from quantbot.learner.forward_source import MAX_CAPTURE_BYTES, Snapshot
from quantbot.learner.models import LearnerModel
from quantbot.learner.paper_runner import PaperExecutionPolicy
from quantbot.research.manifest import content_id
from quantbot.risk.exposure import load_exposure_classification
from quantbot.risk.policy_versions import PolicyVersion
from quantbot.storage import Database
from quantbot.strategy import StrategyConfig, load_strategy_config, strategy_id_for
from quantbot.strategy.identity import validate_strategy_identity

PREDECESSORS = frozenset({"Elrond Five Minute PAPER Entry", "Elrond Adaptive Paper Shadow"})


class EntryLaunchConfig(LearnerModel):
    source_config: Path
    source_config_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    package_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    binding_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy: PaperExecutionPolicy
    mode: Literal["PAPER"] = "PAPER"
    maximum_workers: Literal[2] = 2

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if not self.source_config.is_absolute() or self.source_config.is_symlink():
            raise ValueError("ABSOLUTE_PINNED_SOURCE_REQUIRED")
        if (
            not self.policy.exclusive_learner_entry
            or self.policy.inherited is None
            or self.policy.stop_loss_bps > 100
        ):
            raise ValueError("GRANDFATHERED_BASELINE_AND_BOUNDED_EXCLUSIVE_ENTRY_REQUIRED")
        return self


def configured_source(config: EntryLaunchConfig) -> ExperimentLaunchConfig:
    document = _read_json(config.source_config)
    if hashlib.sha256(config.source_config.read_bytes()).hexdigest() != config.source_config_hash:
        raise ValueError("PINNED_COORDINATOR_CONFIGURATION_CHANGED")
    source = ExperimentLaunchConfig.model_validate(document)
    if source.research_sources is not None:
        # An existing independently commissioned research worker may run, but this
        # initial entry-transfer package must not enable additional fits implicitly.
        raise ValueError("INITIAL_ENTRY_TRANSFER_CANNOT_ENABLE_FITTING")
    return source


def current_cohort(
    store: ExperimentStore,
    manifest: ExperimentManifest,
    *,
    model_id: str,
    now: datetime,
    fresh: bool = True,
) -> tuple[CandidateArtifact, tuple[DecisionRecord, ...]]:
    events = store.read_as_of(manifest.experiment_id, now)
    candidates = [
        row.decode(CandidateArtifact)
        for row in events
        if row.kind == "CandidateArtifact" and row.decode(CandidateArtifact).model_id == model_id
    ]
    if len(candidates) != 1 or candidates[0].manifest_hash != content_id(manifest):
        raise ValueError("EXACT_CANDIDATE_REQUIRED")
    rows = [
        decision
        for row in events
        if row.kind == "DecisionRecord"
        for decision in (row.decode(DecisionRecord),)
        if decision.model_id == model_id
        and decision.recorded_at <= now
        and (not fresh or now < decision.features.target_at)
        and (not fresh or 0 <= (now - decision.features.feature_at).total_seconds() <= 120)
    ]
    if not rows:
        return candidates[0], ()
    cutoff = max(row.features.feature_at for row in rows)
    # prepare_entry also links the same immutable decision under its execution
    # idempotency key. Equal content is one forecast, not a duplicate symbol.
    selected = tuple(
        {row.decision_id: row for row in rows if row.features.feature_at == cutoff}.values()
    )
    if len({row.features.symbol for row in selected}) != len(selected):
        raise ValueError("DUPLICATE_CURRENT_COHORT_SYMBOL")
    return candidates[0], selected


def read_sources(
    root: Path, manifest: ExperimentManifest, decisions: Sequence[DecisionRecord]
) -> dict[str, Snapshot]:
    result = {}
    identities = {row.symbol: row for row in manifest.roster}
    for decision in decisions:
        if not re.fullmatch(r"[a-f0-9]{64}", decision.features.source_snapshot_hash):
            raise ValueError("GENUINE_SOURCE_HASH_REQUIRED")
        identity = identities[decision.features.symbol]
        directory = root / "market" / content_id({"asset_id": identity.asset_id})
        path = (
            directory / "market" / "snapshots" / (decision.features.source_snapshot_hash + ".json")
        )
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CAPTURE_BYTES:
            raise ValueError("GENUINE_SOURCE_ARCHIVE_REQUIRED")
        source = Snapshot.model_validate_json(path.read_bytes())
        if source.identity != decision.features.source_snapshot_hash:
            raise ValueError("GENUINE_SOURCE_HASH_MISMATCH")
        result[identity.symbol] = source
    return result


def entry_ownership_safe(rows: Sequence[Mapping[str, object]]) -> bool:
    return (
        len(rows) == len(PREDECESSORS)
        and {row.get("name") for row in rows} == PREDECESSORS
        and all(row.get("enabled") is False and row.get("state") == "Disabled" for row in rows)
    )


def read_entry_ownership() -> bool:
    # Fixed read-only query: no task arguments, credentials, registration or elevation.
    script = (
        "$ErrorActionPreference='Stop'; $rows=@(); "
        "foreach($name in @('Elrond Five Minute PAPER Entry','Elrond Adaptive Paper Shadow')) { "
        "$t=Get-ScheduledTask -TaskName $name -TaskPath '\\'; "
        "$rows+=@{name=$name; enabled=[bool]$t.Settings.Enabled; state=[string]$t.State} }; "
        "ConvertTo-Json -InputObject $rows -Compress"
    )
    try:
        command = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if command.returncode or len(command.stdout) > 4096:
            return False
        rows = json.loads(command.stdout)
        return isinstance(rows, list) and entry_ownership_safe(rows)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def entry_risk_config(base: StrategyConfig, manifest: ExperimentManifest) -> StrategyConfig:
    return StrategyConfig.model_validate(
        base.model_dump()
        | {
            "universe": tuple(row.symbol for row in manifest.roster) + ("SPY",),
            "roster_size": 1,
            "risk_per_trade_bps": min(
                base.risk_per_trade_bps, manifest.risk.maximum_entry_risk_bps
            ),
            "max_position_value_bps": min(
                base.max_position_value_bps, manifest.risk.maximum_position_bps
            ),
        }
    )


def runtime_baseline_verified(path: Path, base: StrategyConfig) -> bool:
    """Stop the composition root bootstrapping identities or risk policy here."""
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            row = db.execute(
                "SELECT strategy_id,version,git_commit,configuration_hash,deployment_timestamp "
                "FROM strategy_deployments WHERE strategy_id=? AND version=?",
                (strategy_id_for(base), base.version),
            ).fetchone()
            head = db.execute(
                "SELECT * FROM veto_policy_versions ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if row is None or head is None:
                return False
            validate_strategy_identity(base, StrategyIdentity.model_validate(dict(row)))
            document = dict(head)
            document["body"] = json.loads(document["body"])
            document["changes"] = json.loads(document["changes"])
            PolicyVersion.model_validate(document).policy()
        return True
    except (OSError, ValueError, sqlite3.Error):
        return False


class ScheduledEntryReceipt(LearnerModel):
    experiment_id: str
    recorded_at: datetime
    model_id: str
    cohort_hash: str
    status: str
    reason: str
    orders_submitted: int = 0
    run_id: str | None = None
    scope: Literal["PAPER"] = "PAPER"
    synthetic: Literal[False] = False


def _blocked(reason: str) -> dict[str, object]:
    return {"status": "BLOCKED", "reason": reason, "orders_submitted": 0, "scope": "PAPER"}


async def prepare_active_tick(
    config: EntryLaunchConfig,
    source: ExperimentLaunchConfig,
    settings: Settings,
    store: ExperimentStore,
    frozen: ExperimentManifest,
    observation: ObservationBatch | None,
    captures: tuple[Snapshot, ...],
    now: datetime,
) -> PreparedExecution | None:
    """Prepare exact services before the unchanged coordinator journals decisions.

    Recovery-only services cannot request a new entry cycle. Only current genuine
    observations, exact recorded activation and every retained gate enable one.
    """

    def wait(reason: str, *, status: str = "BLOCKED") -> None:
        source.root.mkdir(parents=True, exist_ok=True)
        document = dict(
            status=status,
            reason=reason,
            orders_submitted=0,
            scope="PAPER_ENTRY_PREPARATION",
            measured_at=now.isoformat(),
        )
        temporary = source.root / "entry-receipt.json.tmp"
        temporary.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        temporary.replace(source.root / "entry-receipt.json")

    if (
        settings.TRADING_MODE.value != "PAPER"
        or settings.BROKER_ENVIRONMENT.value != "PAPER"
        or settings.LIVE_TRADING_ACKNOWLEDGED
        or not settings.EXPECTED_ACCOUNT_ID
        or frozen.synthetic
    ):
        wait("PAPER_ONLY_CONFIGURATION_REQUIRED")
        return None
    if not clock_healthy(source.clock_receipt, now):
        wait("CLOCK_SYNC_REQUIRED")
        return None
    activation = active_artifact(store, frozen, now=now)
    if activation is None:
        wait("EXACT_ARTIFACT_ACTIVATION_REQUIRED")
        return None
    if (
        activation.model_id != config.model_id
        or activation.package_id != config.package_id
        or activation.binding_hash != config.binding_hash
    ):
        wait("EXACT_ACTIVATION_CONFIGURATION_MISMATCH")
        return None
    if not read_entry_ownership():
        wait("EXCLUSIVE_ENTRY_TRANSFER_UNVERIFIED")
        return None
    candidate, historical = current_cohort(
        store, frozen, model_id=config.model_id, now=now, fresh=False
    )
    pending = store.pending_intents(frozen.experiment_id)
    fresh = tuple(
        row
        for row in (observation.features if observation else ())
        if candidate.recorded_at <= row.decision_at
        and activation.recorded_at <= row.decision_at
        and 0 <= (now - row.feature_at).total_seconds() <= 120
        and now < row.target_at
    )
    if fresh:
        decisions = tuple(
            DecisionRecord(
                experiment_id=frozen.experiment_id,
                model_id=candidate.model_id,
                features=row,
                recorded_at=now,
                score=score,
                action="BUY" if score > frozen.round_trip_cost_bps / 10000 else "WAIT",
                reason="PAPER_SCORE_ABOVE_COST"
                if score > frozen.round_trip_cost_bps / 10000
                else "SCORE_BELOW_COST",
                activation_id=activation.activation_id,
                paper_eligible=score > frozen.round_trip_cost_bps / 10000,
                synthetic=False,
            )
            for row, score in zip(
                fresh, predict(candidate.artifact, tuple(row.values for row in fresh)), strict=True
            )
        )
    elif pending and historical:
        decisions = historical
    else:
        wait("NO_FRESH_CURRENT_COHORT", status="WAITING")
        return None
    guard = Database(source.root / "guard.db")
    registry = _REGISTRY.set(source.operations_database)
    declarations = _DECLARATIONS.set(source.declaration_database)
    runtime = None
    transferred = False
    try:
        if not all(
            _guard(guard, row.features.feature_at - timedelta(minutes=35), row.features.target_at)
            for row in decisions
        ):
            wait("PROTECTED_OBSERVATION_WINDOW")
            return None
        current_sources = {item.identity: item for item in captures}
        sources = {
            row.features.symbol: current_sources[row.features.source_snapshot_hash]
            for row in decisions
            if row.features.source_snapshot_hash in current_sources
        }
        missing = tuple(row for row in decisions if row.features.symbol not in sources)
        if missing:
            sources.update(read_sources(source.root, frozen, missing))
        from quantbot.runtime import build_runtime, runtime_paths

        paths = runtime_paths()
        if paths.database.resolve() != source.operations_database.resolve():
            wait("EXACT_OPERATIONAL_STORE_REQUIRED")
            return None
        if not runtime_baseline_verified(paths.database, load_strategy_config(paths.config)):
            wait("EXISTING_RUNTIME_BASELINE_REQUIRED_NO_BOOTSTRAP")
            return None
        runtime = build_runtime(settings)
        if runtime.running_git_commit != source.code_commit:
            wait("EXACT_RUNTIME_COMMIT_REQUIRED")
            return None
        runtime.config = entry_risk_config(runtime.config, frozen)

        def clock() -> datetime:
            return datetime.now(UTC)

        # Preserve canonical contexts until coordinator dispatch/entry completes;
        # holdout_guard supplies its own tokens since this function returns first.
        def guarded(start: datetime, end: datetime) -> bool:
            rt = _REGISTRY.set(source.operations_database)
            dt = _DECLARATIONS.set(source.declaration_database)
            try:
                return _guard(guard, start - timedelta(minutes=35), end)
            finally:
                _REGISTRY.reset(rt)
                _DECLARATIONS.reset(dt)

        services = build_pooled_paper_services(
            runtime,
            manifest=frozen,
            candidate=candidate,
            decisions=decisions,
            sources=sources,
            policy=config.policy,
            classification=load_exposure_classification(),
            clock=clock,
            clock_health=lambda stamp: clock_healthy(source.clock_receipt, stamp),
            holdout_guard=guarded,
        )
        if services.runner.binding.configuration_hash != config.binding_hash:
            wait("EXACT_OPERATIONAL_BINDING_REQUIRED")
            return None
        # Pending sends get read/reconcile-only dispatch in the coordinator first.
        # Do not enable a new entry cycle during an unresolved recovery wake.
        context = (
            None
            if pending
            else ActiveCycleContext(
                runtime=runtime, data_sync=services.cycle._data_sync, clock=clock
            )
        )
        unsent = any(row.state == "PENDING" for row in pending)
        wait(
            "PENDING_UNSENT_OPERATOR_REVIEW_REQUIRED"
            if unsent
            else "RECOVERY_BEFORE_NEW_ENTRY"
            if pending
            else "EXACT_ENTRY_SERVICES_PREPARED",
            status="OPERATOR_REVIEW" if unsent else "RECOVERY_ONLY" if pending else "PREPARED",
        )
        transferred = True
        return PreparedExecution(
            runtime=runtime, services=services, context=context, close_guard=guard.close
        )
    finally:
        _REGISTRY.reset(registry)
        _DECLARATIONS.reset(declarations)
        if not transferred:
            guard.close()
            if runtime is not None:
                await runtime.aclose()
                runtime.database.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--config-hash", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    document = _read_json(args.config)
    if hashlib.sha256(args.config.read_bytes()).hexdigest() != args.config_hash:
        raise ValueError("PINNED_ENTRY_CONFIGURATION_CHANGED")
    config = EntryLaunchConfig.model_validate(document)
    source = configured_source(config)
    load_manifest(source)
    verify_checkout(source)
    if args.check:
        print("ADAPTIVE_ENTRY_SOURCE_CHECK_OK_NO_RUNTIME_OR_AUTHORITY_WRITES")
        return 0
    from quantbot.learner.experiment_launch import run_experiment

    async def before_tick(
        tick_source: ExperimentLaunchConfig,
        settings: Settings,
        store: ExperimentStore,
        frozen: ExperimentManifest,
        observation: ObservationBatch | None,
        captures: tuple[Snapshot, ...],
        now: datetime,
    ) -> PreparedExecution | None:
        if (
            hashlib.sha256(args.config.read_bytes()).hexdigest() != args.config_hash
            or configured_source(config) != tick_source
        ):
            raise ValueError("PINNED_ENTRY_CONFIGURATION_CHANGED")
        return await prepare_active_tick(
            config, tick_source, settings, store, frozen, observation, captures, now
        )

    return run_experiment(
        config.source_config,
        once=args.once,
        config_hash=config.source_config_hash,
        prepare_active=before_tick,
    )


def task_xml(
    *,
    repository: str,
    config: str,
    config_hash: str,
    host: str,
    log: str,
    user_id: str,
    commit: str,
) -> str:
    """Least-privilege native plan only; no registration or authority writes."""
    from quantbot.learner.five_minute_launch import task_xml as observation_xml

    if not re.fullmatch(r"[a-f0-9]{64}", config_hash):
        raise ValueError("PINNED_ENTRY_CONFIGURATION_REQUIRED")
    task = ET.fromstring(
        observation_xml(
            repository=repository,
            config=config,
            env_file=config,
            host=host,
            log=log,
            user_id=user_id,
            commit=commit,
        )
    )
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    changes = {
        "t:RegistrationInfo/t:URI": "\\Elrond Adaptive PAPER Entry",
        "t:RegistrationInfo/t:Description": "Elrond explicitly activated adaptive PAPER entry; "
        + commit,
        "t:Settings/t:ExecutionTimeLimit": "PT0S",
    }
    for path, value in changes.items():
        node = task.find(path, ns)
        assert node is not None
        node.text = value
    arguments = task.find("t:Actions/t:Exec/t:Arguments", ns)
    assert arguments is not None and arguments.text is not None
    from pathlib import PureWindowsPath

    wrapper = str(PureWindowsPath(repository) / "deploy" / "omen" / "start-adaptive-entry.ps1")
    arguments.text = (
        f'"{log}" -NoProfile -NonInteractive -WindowStyle Hidden -File "{wrapper}" '
        f'-Repository "{repository}" -Config "{config}" -ConfigHash {config_hash}'
    )
    return ET.tostring(task, encoding="unicode")


if __name__ == "__main__":
    try:
        result = main()
    except Exception:
        print("ADAPTIVE_ENTRY_LAUNCH_REFUSED")
        result = 2
    raise SystemExit(result)
