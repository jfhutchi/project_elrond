"""Explicit five-minute PAPER adapter. No fitting, promotion, or service activation.

The shared runner computes measured portfolio vetoes and durably hands an approved
intent to the independent deadline supervisor. Construction is not commissioning;
the exact deployment, observation gate and ordinary operational lifecycle still apply.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from quantbot.brokers import BrokerAdapter
from quantbot.domain import Position, StrategyIdentity
from quantbot.learner.five_minute import CONTRACT, FEATURE_NAMES, build_features
from quantbot.learner.five_minute_dry_run import ReadOnlyStore, operational_objections
from quantbot.learner.five_minute_forward import FiveMinutePrediction, forecast
from quantbot.learner.five_minute_launch import FiveMinuteLaunchConfig, bound_models, clock_healthy
from quantbot.learner.forward import _read
from quantbot.learner.forward_source import Snapshot, read_snapshot
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
from quantbot.risk.exposure import (
    ExposureClassification,
    ExposureGroup,
    load_exposure_classification,
)
from quantbot.risk.freshness import DataCadence, MarketFreshnessPolicy
from quantbot.risk.policy_versions import recorded_policy_version
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import StrategyConfig

if TYPE_CHECKING:
    from quantbot.runtime import Runtime

DIA_MANDATE = (
    "https://www.ssga.com/us/en/intermediary/etfs/"
    "state-street-spdr-dow-jones-industrial-average-etf-trust-dia"
)


def dia_exposure_classification() -> ExposureClassification:
    """Prospective, issuer-backed US-equity mandate, keeping all prior assignments."""
    base = load_exposure_classification()
    return ExposureClassification(
        version=base.version + 1,
        source=base.source + "; DIA mandate verified 2026-10-01: " + DIA_MANDATE,
        effective_from=max(base.effective_from, date(2026, 10, 1)),
        mandate={**base.mandate, "DIA": frozenset({ExposureGroup.US_EQUITY})},
        factor_model=base.factor_model,
    )


def five_minute_paper_binding(
    model: dict[str, Any],
    *,
    config: StrategyConfig,
    policy: PaperExecutionPolicy,
    veto_policy: PortfolioVetoPolicy,
    market_data: MarketDataSettings,
    classification: ExposureClassification,
) -> PaperModelBinding:
    if (
        model.get("contract") != CONTRACT
        or tuple(model.get("feature_names", ())) != FEATURE_NAMES
        or model.get("training_symbols") != ["DIA"]
    ):
        raise ValueError("separately frozen five-minute DIA contract required")
    root = Path(__file__).parent
    veto = veto_policy.model_dump(mode="json")
    veto["enabled"] = sorted(veto["enabled"])
    return PaperModelBinding.from_config(
        model_id=content_id(model),
        artifact_hash=content_id(model["artifact"]),
        code_hash=content_id(
            {
                name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                for name in (
                    "five_minute.py",
                    "five_minute_forward.py",
                    "five_minute_dry_run.py",
                    "five_minute_paper.py",
                    "five_minute_launch.py",
                    "forward_source.py",
                    "estimators.py",
                    "paper.py",
                    "paper_runner.py",
                    "paper_deadlines.py",
                    "inherited.py",
                )
            }
        ),
        execution_config={
            "contract": CONTRACT,
            "interval_seconds": 300,
            "reference_max_age_seconds": 120,
            "strategy_risk": config.model_dump(mode="json"),
            "execution": policy.model_dump(mode="json"),
            "veto": veto,
            "risk_market_data": asdict(market_data),
            "classification": classification.identity,
            "selection": "positive-cost-adjusted-DIA-one-new-entry",
            "no_model_refit": True,
        },
    )


@dataclass(frozen=True)
class FiveMinuteModelView:
    model_id: str
    training_symbols: tuple[str, ...] = ("DIA",)


class FiveMinutePaperRunner(IntradayPaperRunner):
    """Reuse the established entry/risk/deadline algorithm, never reinterpret 1Min rows."""

    predictions: tuple[FiveMinutePrediction, ...]

    def __init__(
        self,
        *,
        database: Database,
        broker: BrokerAdapter,
        sessions: SessionCalendar,
        model: dict[str, Any],
        predictions: Sequence[FiveMinutePrediction],
        snapshot: Snapshot,
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
        document = json.loads(json.dumps(model))
        validated = tuple(FiveMinutePrediction.model_validate(p.model_dump()) for p in predictions)
        capture = Snapshot.model_validate(snapshot.model_dump())
        if (
            len(validated) != 1
            or config.universe != ("DIA", "SPY")
            or config.calendar != "XNYS"
            or not expected_account_id
            or not policy.exclusive_learner_entry
            or policy.inherited is None
            or policy.stop_loss_bps > 100
            or config.risk_per_trade_bps > 25
            or config.max_position_value_bps > 500
            or str(broker.environment).upper() != "PAPER"
            or capture.query.timeframe != "5Min"
        ):
            raise ValueError(
                "bounded PAPER DIA configuration, baseline and five-minute capture required"
            )
        first = validated[0]
        rows = build_features(
            tuple(i.bar for i in capture.inputs),
            capture.sessions,
            decision_at=first.features.decision_at,
            symbols=("DIA",),
            source_snapshot_hash=capture.identity,
            receipt_at=capture.received_at,
        )
        if (
            rows != tuple(p.features for p in validated)
            or forecast(document, content_id(document), rows, recorded_at=first.recorded_at)
            != validated
        ):
            raise ValueError("five-minute forecast must match original model and provider receipt")
        expected = five_minute_paper_binding(
            document,
            config=config,
            policy=policy,
            veto_policy=veto_policy,
            market_data=market_data,
            classification=classification,
        )
        if binding != expected or identity != binding.strategy_identity(
            git_commit=identity.git_commit, deployed_at=identity.deployment_timestamp
        ):
            raise ValueError("five-minute execution binding must match exact deployment")
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
        self.model = FiveMinuteModelView(content_id(document))
        self.predictions = validated
        self.binding, self.policy, self.account_id = binding, policy, expected_account_id
        self.last_reasons = ()
        self.clock_health, self.holdout_guard = clock_health, holdout_guard
        self._five_document, self._five_capture = document, capture

    def _entry_freshness(self) -> MarketFreshnessPolicy:
        # Report the true feed cadence. Separate entry checks retain the <=120s limit.
        return MarketFreshnessPolicy(
            cadence=DataCadence.INTRADAY, expected_interval_seconds=300, tolerated_intervals=1
        )

    def _pre_handoff_objections(
        self,
        now: datetime,
        positions: Sequence[Position],
    ) -> tuple[str, ...]:
        reasons = []
        if not self.clock_health(now):
            reasons.append("PAPER_VERIFIED_CLOCK_UNAVAILABLE")
        if not all(
            self.holdout_guard(p.features.feature_at, p.features.target_at)
            for p in self.predictions
        ):
            reasons.append("PAPER_PROTECTED_OBSERVATION_WINDOW")
        store = ReadOnlyStore(self._database.path)
        try:
            reasons.extend(
                operational_objections(
                    store, account_id=self.account_id, now=now, position_count=len(positions)
                )
            )
        finally:
            store.close()
        return tuple(reasons)

    async def evaluate(self) -> CycleStrategyResult:
        now = self._clock()
        current = five_minute_paper_binding(
            self._five_document,
            config=self._config,
            policy=self.policy,
            veto_policy=self._veto_policy,
            market_data=self._market_data,
            classification=self._exposure,
        )
        if (
            current != self.binding
            or forecast(
                self._five_document,
                self.model.model_id,
                tuple(p.features for p in self.predictions),
                recorded_at=self.predictions[0].recorded_at,
            )
            != self.predictions
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
            self.holdout_guard(p.features.feature_at, p.features.target_at)
            for p in self.predictions
        ):
            return self._no_entry("PAPER_PROTECTED_OBSERVATION_WINDOW")
        if not all(
            p.recorded_at <= now < p.features.target_at
            and 0
            <= (now - p.features.feature_at).total_seconds()
            <= self.policy.max_reference_age_seconds
            for p in self.predictions
        ):
            return self._no_entry("PAPER_PREDICTION_STALE")
        # Reject below-cost observations before broker refresh or intent writes.
        if all(
            p.score <= max(p.fixed_cost_floor, self.policy.round_trip_cost_bps / 10000)
            for p in self.predictions
        ):
            return self._no_entry("PAPER_SCORE_BELOW_COST_FLOOR")
        if str(self._broker.environment).upper() != "PAPER":
            return self._no_entry("PAPER_BROKER_ENVIRONMENT_MISMATCH")
        health = await self._broker.health_check()
        after = self._clock()
        if after < now:
            return self._no_entry("PAPER_LOCAL_CLOCK_REVERSED")
        if (
            not health.healthy
            or health.reasons
            or health.environment.upper() != "PAPER"
            or health.account_id != self.account_id
        ):
            return self._no_entry("PAPER_BROKER_READINESS_FAILED")
        try:
            return await super().evaluate()
        except StrategyDataError:
            # Missing measured history remains a veto; do not create a fake observation.
            return self._no_entry("PAPER_DAILY_RISK_HISTORY_UNAVAILABLE")


def build_five_minute_paper_services(
    runtime: Runtime,
    *,
    model: dict[str, Any],
    predictions: Sequence[FiveMinutePrediction],
    snapshot: Snapshot,
    policy: PaperExecutionPolicy,
    classification: ExposureClassification,
    clock: Callable[[], datetime],
    clock_health: Callable[[datetime], bool],
    holdout_guard: Callable[[datetime, datetime], bool],
) -> IntradayPaperServices:
    """Compose an already configured PAPER runtime; never deploy, migrate or start it.

    Existing protection and a single independent deadline supervisor must remain running.
    The lifecycle retains recovery, lock, kill switch, data/risk synchronization and the
    promotion/deadline-gated existing coordinator. The caller binds each actual run ID.
    """
    if (
        runtime.settings.TRADING_MODE.value != "PAPER"
        or runtime.settings.BROKER_ENVIRONMENT.value != "PAPER"
        or runtime.settings.LIVE_TRADING_ACKNOWLEDGED
        or runtime.settings.EXPECTED_ACCOUNT_ID is None
        or policy.buying_power_reserve_bps != runtime.settings.ENTRY_BUYING_POWER_RESERVE_BPS
    ):
        raise PaperExecutionNotConfigured(
            "five-minute factory requires exact PAPER account and reserve"
        )
    with runtime.database.transaction() as session:
        repo = StorageRepository(session)
        version = recorded_policy_version(repo)
        if version is None:
            raise PaperExecutionNotConfigured("five-minute recorded risk policy missing")
        binding = five_minute_paper_binding(
            model,
            config=runtime.config,
            policy=policy,
            veto_policy=version.policy(),
            market_data=runtime.market_data_config,
            classification=classification,
        )
        candidate = binding.strategy_identity(
            git_commit=runtime.running_git_commit,
            deployed_at=datetime.fromisoformat(model["created_at"]),
        )
        identity = repo.get_strategy_deployment(candidate.strategy_id, candidate.version)
    if identity is None:
        raise PaperExecutionNotConfigured(
            "five-minute exact deployment missing; no promotion created"
        )
    runner = FiveMinutePaperRunner(
        database=runtime.database,
        broker=runtime.broker,
        sessions=runtime.sessions,
        model=model,
        predictions=predictions,
        snapshot=snapshot,
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
        {**runtime.config.model_dump(), "universe": pilot_risk_symbols(runtime.config, policy)}
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


@dataclass(frozen=True)
class FiveMinuteCohort:
    model: dict[str, Any]
    predictions: tuple[FiveMinutePrediction, ...]
    snapshot: Snapshot


def load_five_minute_cohort(
    config: FiveMinuteLaunchConfig,
    model_id: str,
    *,
    now: datetime,
) -> FiveMinuteCohort | None:
    """Read existing frozen observations; never capture, fit, select a challenger or write.

    Model choice must be explicit and in the already frozen pair. Canonical budget/
    preregistration verification is reused. A stale/missing cohort or clock waits;
    corrupt, duplicate or incompatible evidence refuses rather than relabeling.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("aware actual decision clock required")
    if model_id not in (config.mean_model, config.ridge_model):
        raise ValueError("model must belong to original frozen pair")
    if not clock_healthy(config.clock_receipt, now):
        return None
    model = next((m for m in bound_models(config) if content_id(m) == model_id), None)
    if model is None:
        raise ValueError("original frozen model missing")
    fresh = []
    for path in sorted((config.root / "predictions").glob("*/*.json"))[-100:]:
        p = FiveMinutePrediction.model_validate(_read(path))
        if (
            p.model_id == model_id
            and p.recorded_at <= now < p.features.target_at
            and 0 <= (now - p.features.feature_at).total_seconds() <= 120
        ):
            fresh.append(p)
    if not fresh:
        return None
    latest = max(p.features.feature_at for p in fresh)
    cohort = tuple(p for p in fresh if p.features.feature_at == latest)
    if len(cohort) != 1:
        raise ValueError("unique complete DIA cohort required")
    first = cohort[0]
    source = first.features.source_snapshot_hash
    if len(source) != 64 or any(c not in "0123456789abcdef" for c in source):
        raise ValueError("content-addressed snapshot hash required")
    snapshot = read_snapshot(config.root / "market" / "snapshots" / (source + ".json"))
    if snapshot.query.timeframe != "5Min" or snapshot.identity != source:
        raise ValueError("five-minute provider snapshot binding mismatch")
    rows = build_features(
        tuple(i.bar for i in snapshot.inputs),
        snapshot.sessions,
        decision_at=first.features.decision_at,
        symbols=("DIA",),
        source_snapshot_hash=snapshot.identity,
        receipt_at=snapshot.received_at,
    )
    if (
        rows != tuple(p.features for p in cohort)
        or forecast(model, model_id, rows, recorded_at=first.recorded_at) != cohort
    ):
        raise ValueError("frozen prediction/source proof mismatch")
    return FiveMinuteCohort(model, cohort, snapshot)
