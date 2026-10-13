"""Separate exploration selection through retained PAPER sizing and protection gates.

No model fitting, registration, consent creation, broker submission or promotion here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from quantbot.brokers import BrokerAdapter
from quantbot.domain import Position, StrategyIdentity
from quantbot.learner.experiment_contract import DecisionRecord, ExperimentManifest
from quantbot.learner.experiment_observation import ObservationBatch
from quantbot.learner.experiment_paper import pooled_code_hash
from quantbot.learner.exploration import (
    ExplorationObservation,
    ExplorationPolicy,
    exploration_observations,
    round_robin_observations,
)
from quantbot.learner.five_minute_dry_run import ReadOnlyStore, operational_objections
from quantbot.learner.five_minute_forward import FiveMinutePrediction
from quantbot.learner.five_minute_paper import FiveMinuteModelView
from quantbot.learner.forward_source import Snapshot
from quantbot.learner.intraday import IntradayPrediction
from quantbot.learner.paper import PaperModelBinding, assess_paper_entry, build_learner_paper_cycle
from quantbot.learner.paper_deadlines import PaperDeadlineSupervisor
from quantbot.learner.paper_runner import (
    IntradayCohortSync,
    IntradayPaperRunner,
    IntradayPaperServices,
    PaperExecutionNotConfigured,
    PaperExecutionPolicy,
    PilotRiskDataSync,
    pilot_risk_symbols,
)
from quantbot.operations.cycle import CycleStrategyResult
from quantbot.operations.runners import (
    AdaptiveMomentumRunner,
    MarketDataSettings,
    SessionCalendar,
    StrategyDataError,
)
from quantbot.research.manifest import content_id
from quantbot.risk import PortfolioVetoPolicy
from quantbot.risk.exposure import ExposureClassification
from quantbot.risk.freshness import DataCadence, MarketFreshnessPolicy
from quantbot.risk.policy_versions import recorded_policy_version
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import StrategyConfig

if TYPE_CHECKING:
    from quantbot.runtime import Runtime


def exploration_code_hash() -> str:
    root = Path(__file__).parent
    return content_id({
        "retained_execution": pooled_code_hash(),
        "exploration": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                        for name in ("exploration.py", "exploration_paper.py",
                                     "exploration_execution.py", "exploration_feedback.py",
                                     "exploration_launch.py")},
    })


def exploration_paper_binding(
    manifest: ExperimentManifest,
    exploration: ExplorationPolicy,
    *,
    config: StrategyConfig,
    policy: PaperExecutionPolicy,
    veto_policy: PortfolioVetoPolicy,
    market_data: MarketDataSettings,
    classification: ExposureClassification,
) -> PaperModelBinding:
    manifest = ExperimentManifest.model_validate(manifest.model_dump())
    exploration = ExplorationPolicy.model_validate(exploration.model_dump())
    if (
        exploration.manifest_hash != content_id(manifest)
        or exploration.experiment_id != manifest.experiment_id
        or exploration.synthetic != manifest.synthetic
        or exploration.code_hash != exploration_code_hash()
        or set(config.universe) != {row.symbol for row in manifest.roster} | {"SPY"}
        or config.calendar != "XNYS" or config.roster_size != 1
        or config.risk_per_trade_bps > min(exploration.maximum_entry_risk_bps,
                                         manifest.risk.maximum_entry_risk_bps)
        or config.max_position_value_bps > min(exploration.maximum_position_bps,
                                             manifest.risk.maximum_position_bps)
        or policy.stop_loss_bps != exploration.stop_bps
        or policy.stop_loss_bps > manifest.risk.stop_bps
        or policy.round_trip_cost_bps < manifest.round_trip_cost_bps
        or not policy.exclusive_learner_entry or policy.inherited is None
    ):
        raise ValueError("EXACT_BOUNDED_EXPLORATION_POLICY_REQUIRED")
    veto = veto_policy.model_dump(mode="json")
    veto["enabled"] = sorted(veto["enabled"])
    if not manifest.synthetic and manifest.policy_hash != content_id(veto):
        raise ValueError("EXISTING_FROZEN_RISK_POLICY_REQUIRED")
    return PaperModelBinding.from_config(
        model_id=exploration.policy_id, artifact_hash=content_id(exploration),
        code_hash=exploration_code_hash(), execution_config={
            "manifest_hash": content_id(manifest),
            "exploration_policy": exploration.model_dump(mode="json"),
            "strategy_risk": config.model_dump(mode="json"),
            "execution": policy.model_dump(mode="json"), "veto": veto,
            "risk_market_data": asdict(market_data), "classification": classification.identity,
            "economic_edge_gate": "NOT_APPLICABLE_TO_OUTCOME_FIRST_PAPER_SAMPLING",
            "cost_accounting": "RETAIN_ACTUAL_COSTS_AND_SEPARATE_CONSERVATIVE_COMPARISON",
            "predictive_model": False,
        },
    )


class ExplorationPaperRunner(IntradayPaperRunner):
    observation_kind = "LEARNER_PAPER_EXPLORATION_V1"

    def __init__(
        self, *, database: Database, broker: BrokerAdapter, sessions: SessionCalendar,
        manifest: ExperimentManifest, exploration: ExplorationPolicy,
        observation: ObservationBatch, source: Snapshot | Mapping[str, Snapshot],
        baseline_decisions: Sequence[DecisionRecord], completed_lots: int,
        config: StrategyConfig, identity: StrategyIdentity, binding: PaperModelBinding,
        policy: PaperExecutionPolicy, veto_policy: PortfolioVetoPolicy,
        market_data: MarketDataSettings, classification: ExposureClassification,
        expected_account_id: str, clock: Callable[[], datetime],
        clock_health: Callable[[datetime], bool],
        holdout_guard: Callable[[datetime, datetime], bool],
    ) -> None:
        if str(broker.environment).upper() != "PAPER" or not expected_account_id:
            raise ValueError("EXACT_PAPER_ACCOUNT_REQUIRED")
        self.manifest, self.exploration = manifest, exploration
        self.observation, self.source = observation, source
        self.baseline_decisions, self.completed_lots = tuple(baseline_decisions), completed_lots
        observations = exploration_observations(
            manifest, exploration, observation, source=source,
            baseline_decisions=baseline_decisions, holdout_guard=holdout_guard,
        )
        predictions = round_robin_observations(exploration, observations,
                                              completed_lots=completed_lots)
        expected = exploration_paper_binding(
            manifest, exploration, config=config, policy=policy, veto_policy=veto_policy,
            market_data=market_data, classification=classification,
        )
        if binding != expected or identity != binding.strategy_identity(
            git_commit=identity.git_commit, deployed_at=identity.deployment_timestamp,
        ):
            raise ValueError("EXACT_EXPLORATION_DEPLOYMENT_BINDING_REQUIRED")
        AdaptiveMomentumRunner.__init__(
            self, database=database, broker=broker, config=config, identity=identity,
            sessions=sessions, market_data=market_data, veto_policy=veto_policy,
            exposure_classification=classification,
            entry_buying_power_reserve_bps=policy.buying_power_reserve_bps, clock=clock,
        )
        self.model = FiveMinuteModelView(exploration.policy_id,
                                        tuple(row.symbol for row in manifest.roster))
        self.predictions = predictions
        self.binding, self.policy, self.account_id = binding, policy, expected_account_id
        self.last_reasons = ()
        self.clock_health, self.holdout_guard = clock_health, holdout_guard

    def _entry_candidates(self) -> tuple[ExplorationObservation, ...]:
        return tuple(row for row in self.predictions if isinstance(row, ExplorationObservation))

    def _entry_freshness(self) -> MarketFreshnessPolicy:
        return MarketFreshnessPolicy(cadence=DataCadence.INTRADAY,
                                     expected_interval_seconds=300, tolerated_intervals=1)

    def _pre_handoff_objections(
        self, now: datetime, positions: Sequence[Position],
    ) -> tuple[str, ...]:
        reasons = []
        if not self.clock_health(now):
            reasons.append("PAPER_VERIFIED_CLOCK_UNAVAILABLE")
        if not all(self.holdout_guard(row.features.feature_at - timedelta(minutes=35),
                                      row.features.target_at) for row in self.predictions):
            reasons.append("PAPER_PROTECTED_OBSERVATION_WINDOW")
        store = ReadOnlyStore(self._database.path)
        try:
            reasons.extend(operational_objections(
                store, account_id=self.account_id, now=now, position_count=len(positions),
            ))
        finally:
            store.close()
        return tuple(reasons)

    def _economic_edge_objection(
        self, prediction: IntradayPrediction | FiveMinutePrediction | ExplorationObservation,
        modeled_cost: Decimal | None,
    ) -> str | None:
        if (not isinstance(prediction, ExplorationObservation)
                or prediction.policy_id != self.exploration.policy_id):
            return "EXACT_EXPLORATION_OBSERVATION_REQUIRED"
        return None

    async def evaluate(self) -> CycleStrategyResult:
        now = self._clock()
        current = exploration_paper_binding(
            self.manifest, self.exploration, config=self._config, policy=self.policy,
            veto_policy=self._veto_policy, market_data=self._market_data,
            classification=self._exposure,
        )
        observations = exploration_observations(
            self.manifest, self.exploration, self.observation, source=self.source,
            baseline_decisions=self.baseline_decisions, holdout_guard=self.holdout_guard,
        )
        predictions = round_robin_observations(self.exploration, observations,
                                              completed_lots=self.completed_lots)
        if (current != self.binding or predictions != self.predictions
                or self.model.model_id != self.exploration.policy_id):
            return self._no_entry("PAPER_EXECUTION_BINDING_OR_OBSERVATION_CHANGED")
        gate = assess_paper_entry(
            self._database.path, binding=self.binding, identity=self._identity
        )
        if not gate.allowed:
            return self._no_entry(*gate.reasons)
        if not self.clock_health(now):
            return self._no_entry("PAPER_VERIFIED_CLOCK_UNAVAILABLE")
        if str(self._broker.environment).upper() != "PAPER":
            return self._no_entry("PAPER_BROKER_ENVIRONMENT_MISMATCH")
        health = await self._broker.health_check()
        after = self._clock()
        if after < now or not self.clock_health(after):
            return self._no_entry("PAPER_LOCAL_CLOCK_UNVERIFIED_AFTER_RESPONSE")
        if (not health.healthy or health.reasons or health.environment.upper() != "PAPER"
                or health.account_id != self.account_id):
            return self._no_entry("PAPER_BROKER_READINESS_FAILED")
        try:
            return await IntradayPaperRunner.evaluate(self)
        except StrategyDataError:
            return self._no_entry("PAPER_DAILY_RISK_HISTORY_UNAVAILABLE")


def build_exploration_paper_services(
    runtime: Runtime, *, manifest: ExperimentManifest, exploration: ExplorationPolicy,
    observation: ObservationBatch, source: Snapshot | Mapping[str, Snapshot],
    baseline_decisions: Sequence[DecisionRecord],
    completed_lots: int, policy: PaperExecutionPolicy, classification: ExposureClassification,
    clock: Callable[[], datetime], clock_health: Callable[[datetime], bool],
    holdout_guard: Callable[[datetime, datetime], bool],
) -> IntradayPaperServices:
    """Existing deployment required; never creates authority, consent or a schema."""
    if (
        runtime.settings.TRADING_MODE.value != "PAPER"
        or runtime.settings.BROKER_ENVIRONMENT.value != "PAPER"
        or runtime.settings.LIVE_TRADING_ACKNOWLEDGED
        or runtime.settings.EXPECTED_ACCOUNT_ID is None
        or policy.buying_power_reserve_bps != runtime.settings.ENTRY_BUYING_POWER_RESERVE_BPS
    ):
        raise PaperExecutionNotConfigured("exploration factory requires bounded PAPER settings")
    with runtime.database.transaction() as session:
        repository = StorageRepository(session)
        version = recorded_policy_version(repository)
        if version is None:
            raise PaperExecutionNotConfigured("recorded risk policy required")
        binding = exploration_paper_binding(
            manifest, exploration, config=runtime.config, policy=policy,
            veto_policy=version.policy(), market_data=runtime.market_data_config,
            classification=classification,
        )
        proposed = binding.strategy_identity(git_commit=runtime.running_git_commit,
                                              deployed_at=exploration.recorded_at)
        identity = repository.get_strategy_deployment(proposed.strategy_id, proposed.version)
    if identity is None:
        raise PaperExecutionNotConfigured("exact exploration deployment missing")
    runner = ExplorationPaperRunner(
        database=runtime.database, broker=runtime.broker, sessions=runtime.sessions,
        manifest=manifest, exploration=exploration, observation=observation, source=source,
        baseline_decisions=baseline_decisions, completed_lots=completed_lots,
        config=runtime.config, identity=identity, binding=binding, policy=policy,
        veto_policy=version.policy(), market_data=runtime.market_data_config,
        classification=classification, expected_account_id=runtime.settings.EXPECTED_ACCOUNT_ID,
        clock=clock, clock_health=clock_health, holdout_guard=holdout_guard,
    )
    risk_config = StrategyConfig.model_validate(
        runtime.config.model_dump() | {"universe": pilot_risk_symbols(runtime.config, policy)}
    )
    risk_sync = PilotRiskDataSync(
        database=runtime.database, client=runtime.market_data, sessions=runtime.sessions,
        config=risk_config, market_data=runtime.market_data_config,
        veto_policy=version.policy(), clock=clock,
    )
    cycle = build_learner_paper_cycle(
        runtime, runner, binding=binding, identity=identity,
        max_data_age_seconds=policy.max_reference_age_seconds,
        data_sync=IntradayCohortSync(risk_sync, runner), clock=clock,
        clock_contract=policy.clock_contract,
    )
    deadlines = PaperDeadlineSupervisor(
        database=runtime.database, broker=runtime.broker, coordinator=runtime.coordinator,
        identity=runtime.identity, expected_account_id=runtime.settings.EXPECTED_ACCOUNT_ID,
        lock_path=runtime.paths.lock, clock=clock,
        clock_contract=policy.clock_contract,
    )
    return IntradayPaperServices(runner=runner, cycle=cycle, deadlines=deadlines)
