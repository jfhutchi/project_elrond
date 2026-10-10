"""Frozen pooled PAPER runner around retained sizing, vetoes and independent exits.

Construction never registers a deployment, promotes a model or starts a service.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from quantbot.brokers import BrokerAdapter
from quantbot.domain import StrategyIdentity
from quantbot.learner.estimators import predict
from quantbot.learner.experiment_contract import (
    CandidateArtifact,
    DecisionRecord,
    ExperimentManifest,
)
from quantbot.learner.five_minute import build_features
from quantbot.learner.five_minute_forward import FiveMinutePrediction
from quantbot.learner.five_minute_paper import (
    FiveMinuteModelView,
    FiveMinutePaperRunner,
    five_minute_code_hash,
)
from quantbot.learner.forward_source import Snapshot
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
from quantbot.risk.policy_versions import recorded_policy_version
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import StrategyConfig

if TYPE_CHECKING:
    from quantbot.runtime import Runtime


def pooled_code_hash() -> str:
    root = Path(__file__).parent
    return content_id(
        {
            "retained_execution": five_minute_code_hash(),
            "adaptive": {
                name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                for name in (
                    "experiment_contract.py",
                    "experiment_execution.py",
                    "experiment_activation.py",
                    "experiment_switch.py",
                    "experience_continuity.py",
                    "experiment_coordinator.py",
                    "experiment_evaluation.py",
                    "experiment_paper.py",
                    "experiment_lots.py",
                    "experiment_lot_reader.py",
                    "experiment_rejections.py",
                    "experiment_store.py",
                    "experiment_research.py",
                    "experiment_adaptation.py",
                    "../storage/lot_ownership.py",
                    "../operations/protection.py",
                )
            },
        }
    )


def pooled_predictions(
    manifest: ExperimentManifest,
    candidate: CandidateArtifact,
    decisions: Sequence[DecisionRecord],
    *,
    sources: Mapping[str, Snapshot],
    holdout_guard: Callable[[datetime, datetime], bool] | None = None,
) -> tuple[FiveMinutePrediction, ...]:
    candidate = CandidateArtifact.model_validate(candidate.model_dump())
    if candidate.manifest_hash != content_id(manifest) or candidate.synthetic != manifest.synthetic:
        raise ValueError("EXACT_POOLED_MODEL_MANIFEST_REQUIRED")
    if not manifest.synthetic and holdout_guard is None:
        raise ValueError("CANONICAL_SOURCE_GUARD_REQUIRED")
    roster = {row.symbol for row in manifest.roster}
    seen: set[str] = set()
    result = []
    for source_decision in decisions:
        decision = DecisionRecord.model_validate(source_decision.model_dump())
        row = decision.features
        if row.symbol not in roster:
            raise ValueError("FROZEN_ROSTER_REQUIRED")
        if row.symbol in seen:
            raise ValueError("DUPLICATE_COHORT_SYMBOL")
        seen.add(row.symbol)
        if (
            decision.model_id != candidate.model_id
            or decision.experiment_id != manifest.experiment_id
            or decision.synthetic != manifest.synthetic
            or candidate.recorded_at > row.decision_at
        ):
            raise ValueError("EXACT_POOLED_MODEL_REQUIRED")
        if holdout_guard is not None and not holdout_guard(row.feature_at, row.target_at):
            raise ValueError("PROTECTED_OBSERVATION_WINDOW")
        if row.symbol not in sources:
            raise ValueError("GENUINE_PROVIDER_SOURCE_MISSING")
        source = Snapshot.model_validate(sources[row.symbol].model_dump())
        if (
            source.source_id != "alpaca:iex:raw:5min"
            or source.query.timeframe != "5Min"
            or source.query.feed.value != "iex"
            or source.query.adjustment.value != "raw"
            or source.received_at > decision.recorded_at
            or any(receipt.status_code != 200 for receipt in source.receipts)
        ):
            raise ValueError("GENUINE_PROVIDER_SOURCE_REQUIRED")
        rows = build_features(
            tuple(item.bar for item in source.inputs),
            source.sessions,
            decision_at=row.decision_at,
            symbols=(row.symbol,),
            source_snapshot_hash=source.identity,
            receipt_at=source.received_at,
        )
        score = predict(candidate.artifact, (row.values,))[0]
        if rows != (row,) or score != decision.score:
            raise ValueError("EXACT_POOLED_FORECAST_REQUIRED")
        result.append(
            FiveMinutePrediction(
                model_id=candidate.model_id,
                features=row,
                recorded_at=decision.recorded_at,
                score=score,
                cost_status="ABOVE_FIXED_COST_FLOOR" if score > 0.001 else "BELOW_FIXED_COST_FLOOR",
            )
        )
    if (
        not result
        or len({(row.features.feature_at, row.features.target_at) for row in result}) != 1
    ):
        raise ValueError("ONE_FRESH_COHORT_REQUIRED")
    return tuple(result)


def pooled_paper_binding(
    manifest: ExperimentManifest,
    candidate: CandidateArtifact,
    *,
    config: StrategyConfig,
    policy: PaperExecutionPolicy,
    veto_policy: PortfolioVetoPolicy,
    market_data: MarketDataSettings,
    classification: ExposureClassification,
) -> PaperModelBinding:
    if candidate.manifest_hash != content_id(manifest):
        raise ValueError("EXACT_POOLED_MODEL_MANIFEST_REQUIRED")
    if (
        set(config.universe) != {row.symbol for row in manifest.roster} | {"SPY"}
        or config.calendar != "XNYS"
        or config.roster_size != 1
        or config.risk_per_trade_bps > manifest.risk.maximum_entry_risk_bps
        or config.max_position_value_bps > manifest.risk.maximum_position_bps
        or policy.stop_loss_bps > manifest.risk.stop_bps
        or policy.round_trip_cost_bps < manifest.round_trip_cost_bps
        or not policy.exclusive_learner_entry
        or policy.inherited is None
    ):
        raise ValueError("FROZEN_POOLED_PAPER_RISK_REQUIRED")
    veto = veto_policy.model_dump(mode="json")
    veto["enabled"] = sorted(veto["enabled"])
    if not manifest.synthetic and (
        manifest.code_hash != pooled_code_hash() or manifest.policy_hash != content_id(veto)
    ):
        raise ValueError("FROZEN_EXECUTION_CODE_AND_POLICY_REQUIRED")
    return PaperModelBinding.from_config(
        model_id=candidate.model_id,
        artifact_hash=content_id(candidate.artifact),
        code_hash=pooled_code_hash(),
        execution_config={
            "manifest_hash": content_id(manifest),
            "roster": [row.model_dump(mode="json") for row in manifest.roster],
            "strategy_risk": config.model_dump(mode="json"),
            "execution": policy.model_dump(mode="json"),
            "veto": veto,
            "risk_market_data": asdict(market_data),
            "classification": classification.identity,
            "selection": "cost-cleared-score-descending-symbol-ascending-one-new-lot",
            "interval_seconds": 300,
            "reference_max_age_seconds": 120,
            "no_model_refit": True,
        },
    )


class PooledPaperRunner(FiveMinutePaperRunner):
    """Keep the shared measured-risk entry algorithm and independent exit owner."""

    def __init__(
        self,
        *,
        database: Database,
        broker: BrokerAdapter,
        sessions: SessionCalendar,
        manifest: ExperimentManifest,
        candidate: CandidateArtifact,
        decisions: Sequence[DecisionRecord],
        sources: Mapping[str, Snapshot],
        config: StrategyConfig,
        identity: StrategyIdentity,
        binding: PaperModelBinding,
        policy: PaperExecutionPolicy,
        veto_policy: PortfolioVetoPolicy,
        market_data: MarketDataSettings,
        classification: ExposureClassification,
        expected_account_id: str,
        clock: Callable[[], datetime],
        clock_health: Callable[[datetime], bool],
        holdout_guard: Callable[[datetime, datetime], bool],
    ) -> None:
        if str(broker.environment).upper() != "PAPER" or not expected_account_id:
            raise ValueError("EXACT_PAPER_ACCOUNT_REQUIRED")
        self.manifest, self.candidate = manifest, candidate
        self.decisions, self.sources = tuple(decisions), dict(sources)
        predictions = pooled_predictions(
            manifest, candidate, decisions, sources=sources, holdout_guard=holdout_guard
        )
        expected = pooled_paper_binding(
            manifest,
            candidate,
            config=config,
            policy=policy,
            veto_policy=veto_policy,
            market_data=market_data,
            classification=classification,
        )
        if binding != expected or identity != binding.strategy_identity(
            git_commit=identity.git_commit,
            deployed_at=identity.deployment_timestamp,
        ):
            raise ValueError("EXACT_POOLED_DEPLOYMENT_BINDING_REQUIRED")
        # Initialize only the shared risk base, not the intentionally DIA-only constructor.
        AdaptiveMomentumRunner.__init__(
            self,
            database=database,
            broker=broker,
            config=config,
            identity=identity,
            sessions=sessions,
            market_data=market_data,
            veto_policy=veto_policy,
            exposure_classification=classification,
            entry_buying_power_reserve_bps=policy.buying_power_reserve_bps,
            clock=clock,
        )
        self.model = FiveMinuteModelView(
            candidate.model_id, tuple(row.symbol for row in manifest.roster)
        )
        self.predictions = predictions
        self.binding, self.policy, self.account_id = binding, policy, expected_account_id
        self.last_reasons = ()
        self.clock_health, self.holdout_guard = clock_health, holdout_guard

    async def evaluate(self) -> CycleStrategyResult:
        now = self._clock()
        current = pooled_paper_binding(
            self.manifest,
            self.candidate,
            config=self._config,
            policy=self.policy,
            veto_policy=self._veto_policy,
            market_data=self._market_data,
            classification=self._exposure,
        )
        predictions = pooled_predictions(
            self.manifest,
            self.candidate,
            self.decisions,
            sources=self.sources,
            holdout_guard=self.holdout_guard,
        )
        if (
            current != self.binding
            or predictions != self.predictions
            or self.model.model_id != self.candidate.model_id
        ):
            return self._no_entry("PAPER_EXECUTION_BINDING_OR_FORECAST_CHANGED")
        gate = assess_paper_entry(
            self._database.path, binding=self.binding, identity=self._identity
        )
        if not gate.allowed:
            return self._no_entry(*gate.reasons)
        if not self.clock_health(now):
            return self._no_entry("PAPER_VERIFIED_CLOCK_UNAVAILABLE")
        if not all(
            row.recorded_at <= now < row.features.target_at
            and 0
            <= (now - row.features.feature_at).total_seconds()
            <= self.policy.max_reference_age_seconds
            for row in self.predictions
        ):
            return self._no_entry("PAPER_PREDICTION_STALE")
        if all(row.score <= self.policy.round_trip_cost_bps / 10000 for row in self.predictions):
            return self._no_entry("PAPER_SCORE_BELOW_COST_FLOOR")
        if str(self._broker.environment).upper() != "PAPER":
            return self._no_entry("PAPER_BROKER_ENVIRONMENT_MISMATCH")
        health = await self._broker.health_check()
        after = self._clock()
        if after < now or not self.clock_health(after):
            return self._no_entry("PAPER_LOCAL_CLOCK_UNVERIFIED_AFTER_RESPONSE")
        if (
            not health.healthy
            or health.reasons
            or health.environment.upper() != "PAPER"
            or health.account_id != self.account_id
        ):
            return self._no_entry("PAPER_BROKER_READINESS_FAILED")
        try:
            return await IntradayPaperRunner.evaluate(self)
        except StrategyDataError:
            return self._no_entry("PAPER_DAILY_RISK_HISTORY_UNAVAILABLE")


def build_pooled_paper_services(
    runtime: Runtime,
    *,
    manifest: ExperimentManifest,
    candidate: CandidateArtifact,
    decisions: Sequence[DecisionRecord],
    sources: Mapping[str, Snapshot],
    policy: PaperExecutionPolicy,
    classification: ExposureClassification,
    clock: Callable[[], datetime],
    clock_health: Callable[[datetime], bool],
    holdout_guard: Callable[[datetime, datetime], bool],
) -> IntradayPaperServices:
    if (
        runtime.settings.TRADING_MODE.value != "PAPER"
        or runtime.settings.BROKER_ENVIRONMENT.value != "PAPER"
        or runtime.settings.LIVE_TRADING_ACKNOWLEDGED
        or runtime.settings.EXPECTED_ACCOUNT_ID is None
        or policy.buying_power_reserve_bps != runtime.settings.ENTRY_BUYING_POWER_RESERVE_BPS
    ):
        raise PaperExecutionNotConfigured("pooled factory requires bounded PAPER configuration")
    with runtime.database.transaction() as session:
        repository = StorageRepository(session)
        version = recorded_policy_version(repository)
        if version is None:
            raise PaperExecutionNotConfigured("recorded risk policy required")
        binding = pooled_paper_binding(
            manifest,
            candidate,
            config=runtime.config,
            policy=policy,
            veto_policy=version.policy(),
            market_data=runtime.market_data_config,
            classification=classification,
        )
        proposed = binding.strategy_identity(
            git_commit=runtime.running_git_commit, deployed_at=candidate.recorded_at
        )
        identity = repository.get_strategy_deployment(proposed.strategy_id, proposed.version)
    if identity is None:
        raise PaperExecutionNotConfigured("exact pooled deployment missing; no promotion created")
    runner = PooledPaperRunner(
        database=runtime.database,
        broker=runtime.broker,
        sessions=runtime.sessions,
        manifest=manifest,
        candidate=candidate,
        decisions=decisions,
        sources=sources,
        config=runtime.config,
        identity=identity,
        binding=binding,
        policy=policy,
        veto_policy=version.policy(),
        market_data=runtime.market_data_config,
        classification=classification,
        expected_account_id=runtime.settings.EXPECTED_ACCOUNT_ID,
        clock=clock,
        clock_health=clock_health,
        holdout_guard=holdout_guard,
    )
    risk_config = StrategyConfig.model_validate(
        runtime.config.model_dump() | {"universe": pilot_risk_symbols(runtime.config, policy)}
    )
    risk_sync = PilotRiskDataSync(
        database=runtime.database,
        client=runtime.market_data,
        sessions=runtime.sessions,
        config=risk_config,
        market_data=runtime.market_data_config,
        veto_policy=version.policy(),
        clock=clock,
    )
    cycle = build_learner_paper_cycle(
        runtime,
        runner,
        binding=binding,
        identity=identity,
        max_data_age_seconds=policy.max_reference_age_seconds,
        data_sync=IntradayCohortSync(risk_sync, runner),
        clock=clock,
    )
    deadlines = PaperDeadlineSupervisor(
        database=runtime.database,
        broker=runtime.broker,
        coordinator=runtime.coordinator,
        identity=runtime.identity,
        expected_account_id=runtime.settings.EXPECTED_ACCOUNT_ID,
        lock_path=runtime.paths.lock,
        clock=clock,
    )
    return IntradayPaperServices(runner=runner, cycle=cycle, deadlines=deadlines)
