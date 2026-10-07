"""Long-only intraday forecasts through the existing portfolio sizing and veto engine.

One entry per cycle; any outstanding account BUY blocks further entries. This
deliberately conservative rule avoids treating pending orders as invented holdings
or omitting them from concentration vetoes. Completed minute prices are references,
as daily closes were in V1; they are not represented as executable quotes.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import Field
from sqlalchemy import select

from quantbot.brokers import BrokerAdapter
from quantbot.domain import (
    Bar,
    OrderIntent,
    OrderSide,
    OrderType,
    Position,
    StrategyIdentity,
    TimeInForce,
)
from quantbot.learner.exploration import ExplorationObservation
from quantbot.learner.five_minute_forward import FiveMinutePrediction
from quantbot.learner.inherited import InheritedBaseline, observe_inherited
from quantbot.learner.intraday import IntradayModel, IntradayPrediction, predict_intraday
from quantbot.learner.models import LearnerModel
from quantbot.learner.paper import PaperModelBinding, assess_paper_entry, build_learner_paper_cycle
from quantbot.learner.paper_deadlines import (
    HANDOFF,
    PaperDeadline,
    PaperDeadlineSupervisor,
    record_deadline,
    supervisor_objections,
)
from quantbot.market_data import MarketDataCache
from quantbot.market_data.calendar import latest_completed_session
from quantbot.operations.cycle import (
    CycleStrategyResult,
    DataSyncResult,
    DataSyncRunner,
    RunOnceCycle,
    SubmissionRequest,
)
from quantbot.operations.runners import (
    AdaptiveMomentumRunner,
    MarketDataSettings,
    MarketDataSync,
    SessionCalendar,
    _position_open_times,
)
from quantbot.research.manifest import content_id
from quantbot.risk import (
    EntrySizingRequest,
    OrderPurpose,
    PortfolioVetoPolicy,
    PositionRiskInput,
    RiskApproval,
    build_portfolio_risk_state,
    build_veto_inputs,
    calculate_drawdown,
    evaluate_portfolio_vetoes,
    size_entry,
)
from quantbot.risk.freshness import DataCadence, MarketFreshnessPolicy
from quantbot.risk.policy_versions import recorded_policy_version
from quantbot.storage import Database, StorageRepository
from quantbot.storage.schema import promotion_state
from quantbot.strategy import StrategyConfig, realized_volatility

if TYPE_CHECKING:
    from quantbot.runtime import Runtime


def _now() -> datetime:
    return datetime.now(UTC)


class PaperExecutionPolicy(LearnerModel):
    inherited: InheritedBaseline | None = None
    exclusive_learner_entry: bool = False
    round_trip_cost_bps: int = Field(default=10, ge=10)
    stop_loss_bps: int = Field(default=100, ge=10, le=200)
    max_reference_age_seconds: int = Field(default=120, gt=0, le=120)
    session_exit_buffer_seconds: int = Field(default=120, ge=60)
    buying_power_reserve_bps: int = Field(default=500, ge=0, lt=10000)


def one_minute_code_hash() -> str:
    """Bind the shared recovery implementation as well as the learner entry code."""
    root = Path(__file__).parent
    return content_id(
        {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in (
                "intraday.py",
                "paper.py",
                "paper_runner.py",
                "paper_deadlines.py",
                "inherited.py",
                "execution_worker.py",
                "../execution/snapshot_capture.py",
                "../operations/cycle.py",
                "../execution/recovery.py",
                "../execution/reconciliation.py",
                "../operations/runners.py",
            )
        }
    )


def paper_model_binding(
    model: IntradayModel,
    *,
    config: StrategyConfig,
    policy: PaperExecutionPolicy,
    veto_policy: PortfolioVetoPolicy,
    market_data: MarketDataSettings,
) -> PaperModelBinding:
    veto_document = veto_policy.model_dump(mode="json")
    # Enabled rules are a set: identical controls must bind identically in each process.
    veto_document["enabled"] = sorted(veto_document["enabled"])
    code_hash = one_minute_code_hash()
    return PaperModelBinding.from_config(
        model_id=model.model_id,
        artifact_hash=content_id(model.artifact),
        code_hash=code_hash,
        execution_config={
            "strategy_risk": config.model_dump(mode="json"),
            "execution": policy.model_dump(mode="json"),
            "veto": veto_document,
            "risk_market_data": asdict(market_data),
            "selection": "positive-cost-adjusted-top-quintile-one-entry-per-cycle",
        },
    )


class PaperModelView(Protocol):
    @property
    def model_id(self) -> str: ...
    @property
    def training_symbols(self) -> tuple[str, ...]: ...


class IntradayPaperRunner(AdaptiveMomentumRunner):
    """Use inherited daily-history/stop helpers only for daily risk statistics.

    Selection, temporal checks, intraday freshness and orders below are entirely
    learner-specific. Deadline execution belongs to PaperDeadlineSupervisor, which
    keeps running even when RunOnceCycle cannot evaluate this entry strategy.
    """

    model: PaperModelView
    predictions: tuple[IntradayPrediction | FiveMinutePrediction | ExplorationObservation, ...]
    observation_kind = "LEARNER_PAPER_FORECAST_V1"

    def __init__(
        self,
        *,
        database: Database,
        broker: BrokerAdapter,
        sessions: SessionCalendar,
        model: IntradayModel,
        predictions: Sequence[IntradayPrediction],
        config: StrategyConfig,
        identity: StrategyIdentity,
        binding: PaperModelBinding,
        policy: PaperExecutionPolicy,
        veto_policy: PortfolioVetoPolicy,
        market_data: MarketDataSettings,
        expected_account_id: str,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        verified_model = IntradayModel.model_validate(model.model_dump())
        verified_predictions = tuple(
            IntradayPrediction.model_validate(item.model_dump()) for item in predictions
        )
        if not verified_predictions:
            raise ValueError("PAPER runner requires a complete immutable prediction cohort")
        first = verified_predictions[0]
        if (
            len({item.features.symbol for item in verified_predictions})
            != len(verified_predictions)
            or {item.features.symbol for item in verified_predictions}
            != set(config.universe) - {"SPY"}
            or len(
                {
                    (
                        item.features.decision_at,
                        item.features.target_at,
                        item.recorded_at,
                        item.features.source_snapshot_hash,
                    )
                    for item in verified_predictions
                }
            )
            != 1
        ):
            raise ValueError("PAPER runner requires one complete configured universe cohort")
        reconstructed = predict_intraday(
            verified_model,
            tuple(item.features for item in verified_predictions),
            recorded_at=first.recorded_at,
        )
        if reconstructed != verified_predictions:
            raise ValueError("prediction cohort does not match its immutable model")
        expected = paper_model_binding(
            verified_model,
            config=config,
            policy=policy,
            veto_policy=veto_policy,
            market_data=market_data,
        )
        if binding != expected or identity != binding.strategy_identity(
            git_commit=identity.git_commit, deployed_at=identity.deployment_timestamp
        ):
            raise ValueError("model/code/execution configuration does not match deployment")
        if config.calendar != "XNYS" or not expected_account_id:
            raise ValueError("intraday learner requires XNYS and a pinned PAPER account")
        self.model = verified_model
        self.predictions = verified_predictions
        super().__init__(
            database=database,
            broker=broker,
            config=config,
            identity=identity,
            sessions=sessions,
            market_data=market_data,
            veto_policy=veto_policy,
            entry_buying_power_reserve_bps=policy.buying_power_reserve_bps,
            clock=clock,
        )
        self.binding, self.policy, self.account_id = binding, policy, expected_account_id
        self.last_reasons: tuple[str, ...] = ()

    def _no_entry(self, *reasons: str) -> CycleStrategyResult:
        self.last_reasons = tuple(dict.fromkeys(reasons))
        return CycleStrategyResult(submissions=(), signal_count=len(self.predictions))

    def _entry_freshness(self) -> MarketFreshnessPolicy:
        return MarketFreshnessPolicy(
            cadence=DataCadence.INTRADAY, expected_interval_seconds=60, tolerated_intervals=2
        )

    def _entry_candidates(
        self,
    ) -> tuple[IntradayPrediction | FiveMinutePrediction | ExplorationObservation, ...]:
        forecasts = tuple(row for row in self.predictions
                          if isinstance(row, (IntradayPrediction, FiveMinutePrediction)))
        if len(forecasts) != len(self.predictions):
            raise ValueError("SEPARATE_EXPLORATION_POLICY_REQUIRED")
        ranked = sorted(forecasts, key=lambda item: (-item.score, item.features.symbol))
        return tuple(ranked[:max(1, math.ceil(len(ranked) / 5))])

    def _economic_edge_objection(
        self, prediction: IntradayPrediction | FiveMinutePrediction | ExplorationObservation,
        modeled_cost: Decimal | None,
    ) -> str | None:
        if isinstance(prediction, ExplorationObservation):
            return "SEPARATE_EXPLORATION_POLICY_REQUIRED"
        if prediction.score <= self.policy.round_trip_cost_bps / 10000:
            return "PAPER_SCORE_BELOW_COST_FLOOR"
        if modeled_cost is not None and Decimal(str(prediction.score)) <= modeled_cost:
            return "PAPER_SCORE_BELOW_EXECUTION_COST"
        return None

    def _pre_handoff_objections(
        self,
        now: datetime,
        positions: Sequence[Position],
    ) -> tuple[str, ...]:
        return ()

    def _load_histories(self) -> dict[str, list[Bar]]:
        if not self.policy.exclusive_learner_entry:
            return super()._load_histories()
        provider = MarketDataCache.provider_key(
            self._market_data.provider,
            self._market_data.feed,
            self._market_data.adjustment,
            self._market_data.timeframe,
        )
        with self._database.transaction() as session:
            repo = StorageRepository(session)
            return {
                symbol: repo.list_bars(symbol=symbol, provider=provider)
                for symbol in pilot_risk_symbols(self._config, self.policy)
            }

    async def evaluate_exits(self) -> CycleStrategyResult:
        # This runner has no exit-order authority. Every accepted entry was durably
        # handed to the separate deadline supervisor; emitting another sell here would
        # duplicate its exit. The gateway can stop this runner without stopping exits.
        return self._no_entry("ENTRIES_DISABLED; INDEPENDENT_DEADLINE_SUPERVISOR_OWNS_EXITS")

    async def evaluate(self) -> CycleStrategyResult:
        now = self._clock()
        if self._run_id is None:
            raise ValueError("bind_run must precede learner PAPER evaluation")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("PAPER evaluation requires an aware actual clock")
        gate = assess_paper_entry(
            self._database.path, binding=self.binding, identity=self._identity
        )
        if not gate.allowed:
            return self._no_entry(*gate.reasons)
        if self.policy.exclusive_learner_entry:
            with self._database.transaction() as session:
                other = session.execute(
                    select(promotion_state.c.strategy_id).where(
                        promotion_state.c.strategy_id.startswith("learner-", autoescape=True),
                        promotion_state.c.strategy_id != self._identity.strategy_id,
                        promotion_state.c.stage.in_(
                            ("PAPER_OBSERVATION", "PAPER_QUALIFIED", "LIVE_REVIEW_ELIGIBLE")
                        ),
                    )
                ).first()
            if other is not None:
                return self._no_entry("PAPER_OTHER_LEARNER_ENTRY_ACTIVE")
        heartbeat = supervisor_objections(self._database, account_id=self.account_id, now=now)
        if heartbeat:
            return self._no_entry(*heartbeat)
        if any(
            not item.recorded_at <= now < item.features.target_at
            or not 0
            <= (now - item.features.feature_at).total_seconds()
            <= self.policy.max_reference_age_seconds
            for item in self.predictions
        ):
            return self._no_entry("PAPER_PREDICTION_STALE")
        calendar = await self._sessions.sessions()
        active = next((day for day in calendar if day.open_at <= now < day.close_at), None)
        if active is None:
            return self._no_entry("PAPER_MARKET_CLOSED")
        session_exit = active.close_at - timedelta(seconds=self.policy.session_exit_buffer_seconds)
        if any(item.features.target_at > session_exit for item in self.predictions):
            return self._no_entry("PAPER_HORIZON_EXCEEDS_SESSION")
        market = await self._broker.get_market_clock()
        measured_at = self._clock()
        if measured_at < now:
            return self._no_entry("PAPER_LOCAL_CLOCK_REVERSED")
        now = measured_at
        if not active.open_at <= now < active.close_at:
            return self._no_entry("PAPER_MARKET_CLOSED")
        if any(
            not item.recorded_at <= now < item.features.target_at
            or not 0
            <= (now - item.features.feature_at).total_seconds()
            <= self.policy.max_reference_age_seconds
            for item in self.predictions
        ):
            return self._no_entry("PAPER_PREDICTION_STALE")
        if (
            not market.is_open
            or not 0
            <= (now - market.timestamp).total_seconds()
            <= self.policy.max_reference_age_seconds
        ):
            return self._no_entry("PAPER_BROKER_CLOCK_UNAVAILABLE")
        account = await self._broker.get_account()
        if account.account.account_id != self.account_id:
            return self._no_entry("PAPER_ACCOUNT_MISMATCH")
        if not account.order_entry_allowed or account.restrictions:
            return self._no_entry("PAPER_ACCOUNT_RESTRICTED")
        positions = tuple(
            position for position in await self._broker.get_positions() if position.quantity
        )
        inherited = set()
        if self.policy.inherited is not None:
            inherited = observe_inherited(
                self._database,
                baseline=self.policy.inherited,
                positions=positions,
                account_id=self.account_id,
                identity=self._identity,
                run_id=self._run_id,
                now=now,
                predictions=tuple(row for row in self.predictions
                                  if isinstance(row, (IntradayPrediction, FiveMinutePrediction))),
                cost_bps=self.policy.round_trip_cost_bps,
            )
        if any(
            (self.policy.exclusive_learner_entry or position.symbol in self.model.training_symbols)
            and position.symbol not in inherited
            for position in positions
        ):
            return self._no_entry("PAPER_MODEL_POSITION_LIMIT", "PAPER_SYMBOL_ALREADY_HELD")
        pending = tuple(await self._broker.get_open_orders())
        with self._database.transaction() as session:
            repository = StorageRepository(session)
            version = recorded_policy_version(repository)
            if version is None or version.policy() != self._veto_policy:
                return self._no_entry("PAPER_RECORDED_RISK_POLICY_MISMATCH")
            unresolved = repository.list_unresolved_order_intents()
            equity_history = repository.list_equity_snapshots(account_id=self.account_id)
            fills = repository.list_fills()
        if any(order.side is OrderSide.BUY for order in pending) or any(
            order.side is OrderSide.BUY for order in unresolved
        ):
            return self._no_entry("PAPER_PENDING_BUY_RISK")
        considered = (
            equity_history[-self._config.drawdown_lookback_sessions :]
            if self._config.drawdown_lookback_sessions > 0
            else equity_history
        )
        high_water = max((row.equity for row in considered), default=None)
        drawdown = calculate_drawdown(account.account.equity, high_water, self._config)
        if drawdown.entry_halted:
            return self._no_entry("DRAWDOWN_ENTRY_HALT")
        completed = latest_completed_session(calendar, now)
        if completed is None:
            return self._no_entry("PAPER_DAILY_RISK_HISTORY_UNAVAILABLE")
        histories = self._slice(self._load_histories(), completed.close_at)
        dependence = self._dependence_snapshot(
            histories,
            [day.session_date for day in calendar if day.close_at <= now][
                -self._veto_policy.correlation_lookback_sessions :
            ],
        )
        opened = _position_open_times(fills)
        top = self._entry_candidates()
        rejections: list[str] = []
        for prediction in top:
            symbol = prediction.features.symbol
            economic_objection = self._economic_edge_objection(prediction, None)
            if economic_objection:
                rejections.append(economic_objection)
                continue
            if any(position.symbol == symbol for position in positions):
                rejections.append("PAPER_SYMBOL_ALREADY_HELD")
                continue
            if any(order.symbol == symbol for order in (*pending, *unresolved)):
                rejections.append("PAPER_SYMBOL_ORDER_PENDING")
                continue
            digest = content_id(
                {"binding": self.binding.configuration_hash, "prediction": prediction.prediction_id}
            )
            intent_id = "learner-entry-" + digest
            with self._database.transaction() as session:
                repository = StorageRepository(session)
                if repository.get_order_intent(intent_id) is not None or _already_handed_off(
                    repository, self._identity.strategy_id, intent_id
                ):
                    rejections.append("PAPER_PREDICTION_ALREADY_HANDED_OFF")
                    continue
                inputs = [
                    PositionRiskInput(
                        position=position,
                        active_stop=self._known_stop(
                            repository, position, opened.get(position.symbol)
                        ),
                    )
                    for position in positions
                ]
                portfolio = build_portfolio_risk_state(
                    content_id({"account": self.account_id, "at": now}),
                    account.account,
                    symbol,
                    inputs,
                    (),
                    buying_power_reserve_bps=self.policy.buying_power_reserve_bps,
                )
                reference = Decimal(str(prediction.features.reference_close))
                stop_distance = reference * Decimal(self.policy.stop_loss_bps) / Decimal(10000)
                sized = size_entry(
                    EntrySizingRequest(
                        symbol=symbol,
                        reference_price=reference,
                        stop_distance=stop_distance,
                        portfolio=portfolio,
                        drawdown=drawdown,
                        realized_volatility=(
                            realized_volatility(
                                histories[symbol], self._config.volatility_lookback_days
                            )
                            if self._config.volatility_target_bps
                            else None
                        ),
                    ),
                    self._config,
                )
                if not isinstance(sized, RiskApproval):
                    rejections.extend(sized.reasons)
                    continue
                modeled_cost = Decimal(2 * max(5, self._config.slippage_bps)) / Decimal(
                    10000
                ) + 2 * self._config.commission_per_order / (sized.quantity * reference)
                economic_objection = self._economic_edge_objection(prediction, modeled_cost)
                if economic_objection:
                    rejections.append(economic_objection)
                    continue
                freshness = self._entry_freshness()
                veto_inputs = build_veto_inputs(
                    symbol=symbol,
                    equity=account.account.equity,
                    positions=positions,
                    order_quantity=sized.quantity,
                    order_price=reference,
                    equity_marks=[(row.captured_at, row.equity) for row in equity_history],
                    symbol_bars=[(bar.timestamp, bar.volume) for bar in histories.get(symbol, ())],
                    observed_at=now,
                    volume_sessions=self._veto_policy.minimum_volume_sessions,
                    classification=self._exposure,
                    sessions=calendar,
                    freshness_policy=freshness,
                    dependence=dependence,
                    correlation_threshold=self._veto_policy.correlation_threshold,
                ).model_copy(update={"latest_market_data_at": prediction.features.feature_at})
                # Liquidity/dependence retain completed daily observations; freshness uses
                # the actual minute feature timestamp, without inventing a volume observation.
                vetoes = evaluate_portfolio_vetoes(veto_inputs, self._veto_policy)
                if not vetoes.allowed:
                    rejections.extend(vetoes.reasons)
                    continue
                issued_at = self._clock()
                valid_until = prediction.features.feature_at + timedelta(
                    seconds=self.policy.max_reference_age_seconds
                )
                if not now <= issued_at < min(valid_until, prediction.features.target_at):
                    return self._no_entry("PAPER_PREDICTION_STALE")
                objections = self._pre_handoff_objections(issued_at, positions)
                if objections:
                    return self._no_entry(*objections)
                entry = OrderIntent(
                    intent_id=intent_id,
                    client_order_id="qb-" + digest[:40],
                    strategy_id=self._identity.strategy_id,
                    signal_date=active.session_date,
                    symbol=symbol,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    time_in_force=TimeInForce.DAY,
                    quantity=sized.quantity,
                    created_at=issued_at,
                )
                repository.record_signal(
                    "learner-forecast-" + prediction.prediction_id,
                    run_id=self._run_id,
                    strategy_id=self._identity.strategy_id,
                    symbol=symbol,
                    occurred_at=issued_at,
                    payload={
                        "kind": self.observation_kind,
                        "prediction": prediction.model_dump(mode="json"),
                        "cohort_hash": content_id(self.predictions),
                        "binding": self.binding.model_dump(),
                        "risk_approval": sized.model_dump(mode="json"),
                        "vetoes": vetoes.model_dump(mode="json"),
                        "modeled_round_trip_cost": str(modeled_cost),
                    },
                )
            record_deadline(
                self._database,
                PaperDeadline(
                    entry_intent=entry,
                    binding=self.binding,
                    identity=self._identity,
                    model_id=self.model.model_id,
                    prediction_id=prediction.prediction_id,
                    account_id_hash=PaperDeadline.account_hash(self.account_id),
                    recorded_at=issued_at,
                    reference_at=prediction.features.feature_at,
                    entry_valid_until=valid_until,
                    deadline_at=min(prediction.features.target_at, now + timedelta(minutes=30)),
                    session_exit_at=session_exit,
                    initial_stop=reference - stop_distance,
                ),
                run_id=self._run_id,
            )
            self.last_reasons = ()
            return CycleStrategyResult(
                submissions=(
                    SubmissionRequest(
                        intent=entry,
                        purpose=OrderPurpose.ENTRY,
                        market_eligible=True,
                        drawdown=drawdown,
                        risk_approved=True,
                        vetoes=vetoes,
                    ),
                ),
                signal_count=len(self.predictions),
            )
        return self._no_entry(*rejections)


def _already_handed_off(repository: StorageRepository, strategy_id: str, intent_id: str) -> bool:
    for row in repository.list_signals(strategy_id=strategy_id):
        document = row.payload.get("document")
        if row.payload.get("kind") != HANDOFF or not isinstance(document, dict):
            continue
        entry = document.get("entry_intent")
        if isinstance(entry, dict) and entry.get("intent_id") == intent_id:
            return True
    return False


def pilot_risk_symbols(config: StrategyConfig, policy: PaperExecutionPolicy) -> tuple[str, ...]:
    inherited = (
        () if policy.inherited is None else tuple(p.symbol for p in policy.inherited.positions)
    )
    return tuple(sorted(set(config.universe) | set(inherited)))


class PilotRiskDataSync(MarketDataSync):
    """Separate universe cursor; adding DIA cannot borrow the existing daily checkpoint."""

    @property
    def source_id(self) -> str:
        return super().source_id + ":pilot:" + self._configuration_hash()[:16]


class IntradayCohortSync:
    """Preserve operational data/risk health while checking the issued minute cohort.

    The underlying daily synchronizer still refreshes the history the risk controls
    actually use. Its daily timestamp is not an intraday observation. The separately
    issued immutable forecasts provide the minute cutoff; a stale cohort fails closed.
    """

    def __init__(self, risk_sync: DataSyncRunner, runner: IntradayPaperRunner) -> None:
        self._risk_sync, self._runner = risk_sync, runner

    async def sync(self) -> DataSyncResult:
        risk = await self._risk_sync.sync()
        now = self._runner._clock()
        oldest = min(item.features.feature_at for item in self._runner.predictions)
        valid = all(
            item.recorded_at <= now < item.features.target_at for item in self._runner.predictions
        )
        valid = (
            valid
            and 0 <= (now - oldest).total_seconds() <= self._runner.policy.max_reference_age_seconds
        )
        return risk.model_copy(
            update={
                "healthy": risk.healthy and valid,
                "completed_through": oldest,
                "reasons": risk.reasons + (() if valid else ("PAPER_PREDICTION_STALE",)),
            }
        )


class PaperExecutionNotConfigured(RuntimeError):
    """Production must configure and earn the exact deployment before this factory opens."""


@dataclass(frozen=True)
class IntradayPaperServices:
    runner: IntradayPaperRunner
    cycle: RunOnceCycle
    deadlines: PaperDeadlineSupervisor


def build_intraday_paper_services(
    runtime: Runtime,
    *,
    model: IntradayModel,
    predictions: Sequence[IntradayPrediction],
    policy: PaperExecutionPolicy | None = None,
    clock: Callable[[], datetime] = _now,
) -> IntradayPaperServices:
    """Wire existing collaborators without opening, migrating or changing a deployment.

    Run ``deadlines.serve(stop)`` independently of entry cycles and keep the existing
    position protection service enabled. Bind each actual cycle's run id to ``runner``.
    No supervisor heartbeat means no entry, even if the caller omits the service loop.
    ``Database`` compatibility remains the runtime owner's prerequisite; this factory
    never upgrades a schema-28 store to make a development checkout able to open it.
    """
    policy = policy or PaperExecutionPolicy(
        buying_power_reserve_bps=runtime.settings.ENTRY_BUYING_POWER_RESERVE_BPS
    )
    if policy.buying_power_reserve_bps != runtime.settings.ENTRY_BUYING_POWER_RESERVE_BPS:
        raise PaperExecutionNotConfigured(
            "PAPER_EXECUTION_NOT_CONFIGURED: buying-power reserve differs"
        )
    if runtime.settings.EXPECTED_ACCOUNT_ID is None:
        raise PaperExecutionNotConfigured(
            "PAPER_EXECUTION_NOT_CONFIGURED: account identity missing"
        )
    with runtime.database.transaction() as session:
        repository = StorageRepository(session)
        version = recorded_policy_version(repository)
        if version is None:
            raise PaperExecutionNotConfigured(
                "PAPER_EXECUTION_NOT_CONFIGURED: recorded risk policy missing"
            )
        binding = paper_model_binding(
            model,
            config=runtime.config,
            policy=policy,
            veto_policy=version.policy(),
            market_data=runtime.market_data_config,
        )
        provisional = binding.strategy_identity(
            git_commit=runtime.running_git_commit, deployed_at=model.created_at
        )
        identity = repository.get_strategy_deployment(provisional.strategy_id, provisional.version)
    if identity is None:
        raise PaperExecutionNotConfigured(
            "PAPER_EXECUTION_NOT_CONFIGURED: exact learner deployment missing"
        )
    runner = IntradayPaperRunner(
        database=runtime.database,
        broker=runtime.broker,
        sessions=runtime.sessions,
        model=model,
        predictions=predictions,
        config=runtime.config,
        identity=identity,
        binding=binding,
        policy=policy,
        veto_policy=version.policy(),
        market_data=runtime.market_data_config,
        expected_account_id=runtime.settings.EXPECTED_ACCOUNT_ID,
        clock=clock,
    )
    risk_sync = runtime.data_sync
    if policy.exclusive_learner_entry:
        risk_config = runtime.config.model_copy(
            update={"universe": pilot_risk_symbols(runtime.config, policy)}
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
