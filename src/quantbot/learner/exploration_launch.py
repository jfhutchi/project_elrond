"""Explicitly commissioned outcome-first PAPER launcher, never a fitted-model approval.

Reuses the pinned coordinator, existing credentials and retained service graph.
Cannot create consent, deployment, promotion, task ownership or a live route.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from quantbot.config import Settings
from quantbot.learner.estimators import predict
from quantbot.learner.experiment_contract import (
    CandidateArtifact,
    DecisionRecord,
    ExperimentManifest,
)
from quantbot.learner.experiment_coordinator import ExplorationCycleContext
from quantbot.learner.experiment_entry import read_entry_ownership, runtime_baseline_verified
from quantbot.learner.experiment_launch import (
    ExperimentLaunchConfig,
    PreparedExecution,
    _read_json,
    load_manifest,
    run_experiment,
    verify_checkout,
)
from quantbot.learner.experiment_observation import ObservationBatch
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.exploration import ExplorationPolicy
from quantbot.learner.exploration_execution import ExplorationCommission, verified_commission
from quantbot.learner.exploration_feedback import verified_feedback
from quantbot.learner.exploration_paper import build_exploration_paper_services
from quantbot.learner.five_minute_launch import clock_healthy
from quantbot.learner.forward import _DECLARATIONS, _REGISTRY, _guard
from quantbot.learner.forward_source import MAX_CAPTURE_BYTES, Snapshot
from quantbot.learner.models import LearnerModel
from quantbot.learner.paper_runner import PaperExecutionPolicy
from quantbot.research.manifest import content_id
from quantbot.risk.exposure import load_exposure_classification
from quantbot.storage import Database
from quantbot.strategy import StrategyConfig, load_strategy_config


class ExplorationLaunchConfig(LearnerModel):
    source_config: Path
    source_config_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_path: Path
    policy_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    binding_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy: PaperExecutionPolicy
    mode: Literal["PAPER"] = "PAPER"
    maximum_workers: Literal[2] = 2

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if any(
            not path.is_absolute() or path.is_symlink()
            for path in (self.source_config, self.policy_path)
        ):
            raise ValueError("ABSOLUTE_PINNED_SOURCE_REQUIRED")
        if (
            not self.policy.exclusive_learner_entry
            or self.policy.inherited is None
            or self.policy.stop_loss_bps > 100
        ):
            raise ValueError("GRANDFATHERED_BASELINE_AND_BOUNDED_EXCLUSIVE_ENTRY_REQUIRED")
        return self


def read_policy(config: ExplorationLaunchConfig) -> ExplorationPolicy:
    policy = ExplorationPolicy.model_validate(_read_json(config.policy_path))
    if policy.synthetic or policy.policy_id != config.policy_id:
        raise ValueError("EXACT_REAL_EXPLORATION_POLICY_REQUIRED")
    return policy


def configured_source(config: ExplorationLaunchConfig) -> ExperimentLaunchConfig:
    document = _read_json(config.source_config)
    if hashlib.sha256(config.source_config.read_bytes()).hexdigest() != config.source_config_hash:
        raise ValueError("PINNED_COORDINATOR_CONFIGURATION_CHANGED")
    source = ExperimentLaunchConfig.model_validate(document)
    if source.research_sources is not None:
        raise ValueError("INITIAL_ENTRY_TRANSFER_CANNOT_ENABLE_FITTING")
    return source


def exploration_risk_config(
    base: StrategyConfig,
    manifest: ExperimentManifest,
    policy: ExplorationPolicy,
) -> StrategyConfig:
    return StrategyConfig.model_validate(
        base.model_dump()
        | {
            "universe": tuple(row.symbol for row in manifest.roster) + ("SPY",),
            "roster_size": 1,
            "risk_per_trade_bps": min(
                base.risk_per_trade_bps,
                manifest.risk.maximum_entry_risk_bps,
                policy.maximum_entry_risk_bps,
            ),
            "max_position_value_bps": min(
                base.max_position_value_bps,
                manifest.risk.maximum_position_bps,
                policy.maximum_position_bps,
            ),
        }
    )


def baseline_diagnostics(
    store: ExperimentStore,
    manifest: ExperimentManifest,
    observation: ObservationBatch,
    *,
    now: datetime,
) -> tuple[DecisionRecord, ...]:
    """Preserve genuine predictions; negative scores are diagnostics, never rewritten."""
    events = store.read_as_of(manifest.experiment_id, now)
    candidates = {
        row.model_id: row
        for event in events
        if event.kind == "CandidateArtifact"
        for row in (event.decode(CandidateArtifact),)
        if row.manifest_hash == content_id(manifest)
        and row.recorded_at <= min(item.decision_at for item in observation.features)
    }
    existing = {
        content_id((row.model_id, row.features)): row
        for event in events
        if event.kind == "DecisionRecord"
        for row in (event.decode(DecisionRecord),)
    }
    rows = []
    for candidate in sorted(candidates.values(), key=lambda row: row.model_id):
        for feature, score in zip(
            observation.features,
            predict(candidate.artifact, tuple(row.values for row in observation.features)),
            strict=True,
        ):
            previous = existing.get(content_id((candidate.model_id, feature)))
            if previous is not None:
                if previous.score != score:
                    raise ValueError("IMMUTABLE_BASELINE_SCORE_MISMATCH")
                rows.append(previous)
            elif (
                observation.recorded_at <= feature.decision_at
                and now < feature.target_at
                and 0 <= (now - feature.feature_at).total_seconds() <= 120
            ):
                row = DecisionRecord(
                    experiment_id=manifest.experiment_id,
                    recorded_at=observation.recorded_at,
                    model_id=candidate.model_id,
                    features=feature,
                    score=score,
                    action="WAIT",
                    reason="EXPLORATION_BASELINE_DIAGNOSTIC",
                    paper_eligible=False,
                    synthetic=manifest.synthetic,
                )
                store.append(row, idempotency_key="exploration-baseline:" + row.decision_id)
                rows.append(row)
    return tuple(rows)


def archived_capture(
    source: ExperimentLaunchConfig,
    frozen: ExperimentManifest,
    observation: ObservationBatch,
    *,
    symbol: str | None = None,
) -> Snapshot:
    """Guard the entire possible query day before opening an archived market file."""
    feature = next(row for row in observation.features if symbol is None or row.symbol == symbol)
    day = feature.feature_at.astimezone(UTC).date()
    start, end = datetime.combine(day, time.min, UTC), datetime.combine(day, time.max, UTC)
    guard = Database(source.root / "guard.db")
    registry, declarations = (
        _REGISTRY.set(source.operations_database),
        _DECLARATIONS.set(source.declaration_database),
    )
    try:
        if not _guard(guard, start, end) or not _guard(
            guard, feature.feature_at - timedelta(minutes=35), feature.target_at
        ):
            raise ValueError("PROTECTED_OBSERVATION_WINDOW")
        identity = next(row for row in frozen.roster if row.symbol == feature.symbol)
        path = (
            source.root
            / "market"
            / content_id({"asset_id": identity.asset_id})
            / "market"
            / "snapshots"
            / (feature.source_snapshot_hash + ".json")
        )
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CAPTURE_BYTES:
            raise ValueError("GENUINE_SOURCE_ARCHIVE_REQUIRED")
        capture = Snapshot.model_validate_json(path.read_bytes())
        if (
            capture.identity != feature.source_snapshot_hash
            or not start <= capture.query.start <= capture.query.end <= end
        ):
            raise ValueError("EXACT_GUARDED_ARCHIVE_QUERY_REQUIRED")
        return capture
    finally:
        _REGISTRY.reset(registry)
        _DECLARATIONS.reset(declarations)
        guard.close()


def resolve_sources(
    source: ExperimentLaunchConfig,
    frozen: ExperimentManifest,
    observation: ObservationBatch,
    captures: tuple[Snapshot, ...],
) -> dict[str, Snapshot]:
    snapshots = {capture.identity: capture for capture in captures}
    result = {}
    for feature in observation.features:
        capture = snapshots.get(feature.source_snapshot_hash)
        if capture is None:
            capture = archived_capture(source, frozen, observation, symbol=feature.symbol)
        if capture.identity != feature.source_snapshot_hash:
            raise ValueError("GENUINE_SOURCE_HASH_MISMATCH")
        result[feature.symbol] = capture
    return result


def complete_cohort(manifest: ExperimentManifest, observation: ObservationBatch | None) -> bool:
    roster = {row.symbol for row in manifest.roster}
    return bool(observation is not None and len(observation.features) == len(roster)
                and {row.symbol for row in observation.features} == roster
                and len(observation.coverage) == len(roster)
                and {row.symbol for row in observation.coverage} == roster
                and all(row.status == "AVAILABLE" for row in observation.coverage)
                and len({(row.feature_at, row.target_at) for row in observation.features}) == 1)


async def prepare_exploration_tick(
    config: ExplorationLaunchConfig,
    source: ExperimentLaunchConfig,
    settings: Settings,
    store: ExperimentStore,
    frozen: ExperimentManifest,
    observation: ObservationBatch | None,
    captures: tuple[Snapshot, ...],
    now: datetime,
) -> PreparedExecution | None:
    def wait(reason: str, status: str = "BLOCKED") -> None:
        source.root.mkdir(parents=True, exist_ok=True)
        path = source.root / "entry-receipt.json.tmp"
        path.write_text(
            json.dumps(
                dict(
                    status=status,
                    reason=reason,
                    orders_submitted=0,
                    scope="PAPER_EXPLORATION_PREPARATION",
                    measured_at=now.isoformat(),
                ),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        path.replace(source.root / "entry-receipt.json")

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
    policy = read_policy(config)
    events = store.read_as_of(frozen.experiment_id, now)
    commissions = tuple(
        event.decode(ExplorationCommission)
        for event in events
        if event.kind == "ExplorationCommission"
        and event.decode(ExplorationCommission).policy_id == policy.policy_id
    )
    if (
        len(commissions) != 1
        or commissions[0].binding_hash != config.binding_hash
        or commissions[0].code_commit != source.code_commit
        or commissions[0].manifest_hash != content_id(frozen)
    ):
        wait("EXACT_OPERATOR_EXPLORATION_COMMISSION_REQUIRED")
        return None
    if not read_entry_ownership():
        wait("EXCLUSIVE_ENTRY_TRANSFER_UNVERIFIED")
        return None
    pending = store.pending_intents(frozen.experiment_id)
    if not complete_cohort(frozen, observation):
        if not pending:
            wait("NO_FRESH_CURRENT_COHORT", "WAITING")
            return None
        batches = tuple(
            event.decode(ObservationBatch)
            for event in events
            if event.kind == "ObservationBatch"
            and complete_cohort(frozen, event.decode(ObservationBatch))
        )
        if not batches:
            wait("PENDING_INTENT_SOURCE_UNVERIFIED")
            return None
        observation = max(batches, key=lambda row: row.recorded_at)
    assert observation is not None  # Complete current or complete historical recovery cohort.
    fresh = all(
        commissions[0].recorded_at <= row.decision_at
        and 0 <= (now - row.feature_at).total_seconds() <= 120
        and now < row.target_at
        for row in observation.features
    )
    if not fresh and not pending:
        wait("NO_FRESH_CURRENT_COHORT", "WAITING")
        return None
    try:
        captures_by_symbol = resolve_sources(source, frozen, observation, captures)
    except ValueError:
        wait("GENUINE_GUARDED_SOURCE_ARCHIVE_REQUIRED")
        return None
    guard = Database(source.root / "guard.db")
    registry = _REGISTRY.set(source.operations_database)
    declarations = _DECLARATIONS.set(source.declaration_database)
    runtime = None
    transferred = False
    try:
        from quantbot.runtime import build_runtime, runtime_paths

        paths = runtime_paths()
        if (
            paths.database.resolve() != source.operations_database.resolve()
            or not runtime_baseline_verified(paths.database, load_strategy_config(paths.config))
        ):
            wait("EXISTING_RUNTIME_BASELINE_REQUIRED_NO_BOOTSTRAP")
            return None
        runtime = build_runtime(settings)
        if runtime.running_git_commit != source.code_commit:
            wait("EXACT_RUNTIME_COMMIT_REQUIRED")
            return None
        runtime.config = exploration_risk_config(runtime.config, frozen, policy)

        def clock() -> datetime:
            return datetime.now(UTC)

        def guarded(start: datetime, end: datetime) -> bool:
            rt, dt = (
                _REGISTRY.set(source.operations_database),
                _DECLARATIONS.set(source.declaration_database),
            )
            try:
                return _guard(guard, start, end)
            finally:
                _REGISTRY.reset(rt)
                _DECLARATIONS.reset(dt)

        services = build_exploration_paper_services(
            runtime,
            manifest=frozen,
            exploration=policy,
            observation=observation,
            source=captures_by_symbol,
            baseline_decisions=baseline_diagnostics(store, frozen, observation, now=now),
            completed_lots=len(verified_feedback(store, policy, now=now)),
            policy=config.policy,
            classification=load_exposure_classification(),
            clock=clock,
            clock_health=lambda stamp: clock_healthy(source.clock_receipt, stamp),
            holdout_guard=guarded,
        )
        verified_commission(store, policy, services, now=now)
        if services.runner.binding.configuration_hash != config.binding_hash:
            wait("EXACT_OPERATIONAL_BINDING_REQUIRED")
            return None
        context = (
            None
            if pending or not fresh
            else ExplorationCycleContext(
                runtime=runtime, data_sync=services.cycle._data_sync, clock=clock, policy=policy
            )
        )
        wait(
            "RECOVERY_BEFORE_NEW_ENTRY" if pending else "EXACT_EXPLORATION_SERVICES_PREPARED",
            "RECOVERY_ONLY" if pending else "PREPARED",
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


def task_xml(**kwargs: str) -> str:
    """Native limited-identity plan only, no registration or authority creation."""
    from xml.etree import ElementTree as ET

    from quantbot.learner.experiment_entry import task_xml as entry_task

    task = ET.fromstring(entry_task(**kwargs))
    namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    changes = {"t:RegistrationInfo/t:URI": "\\Elrond PAPER Exploration",
               "t:RegistrationInfo/t:Description": "Elrond outcome-first PAPER exploration; "
               + kwargs["commit"]}
    for path, value in changes.items():
        node = task.find(path, namespace)
        assert node is not None
        node.text = value
    arguments = task.find("t:Actions/t:Exec/t:Arguments", namespace)
    assert arguments is not None and arguments.text is not None
    arguments.text += " -Exploration"
    return ET.tostring(task, encoding="unicode")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--config-hash", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    config = ExplorationLaunchConfig.model_validate(_read_json(args.config))
    if hashlib.sha256(args.config.read_bytes()).hexdigest() != args.config_hash:
        raise ValueError("PINNED_EXPLORATION_CONFIGURATION_CHANGED")
    source = configured_source(config)
    read_policy(config)
    load_manifest(source)
    verify_checkout(source)
    if args.check:
        print("EXPLORATION_SOURCE_CHECK_OK_NO_AUTHORITY_OR_RUNTIME_WRITES")
        return 0

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
            raise ValueError("PINNED_EXPLORATION_CONFIGURATION_CHANGED")
        return await prepare_exploration_tick(
            config, tick_source, settings, store, frozen, observation, captures, now
        )

    return run_experiment(
        config.source_config,
        once=args.once,
        config_hash=config.source_config_hash,
        prepare_active=before_tick,
    )


if __name__ == "__main__":
    try:
        result = main()
    except Exception:
        print("EXPLORATION_LAUNCH_REFUSED")
        result = 2
    raise SystemExit(result)
