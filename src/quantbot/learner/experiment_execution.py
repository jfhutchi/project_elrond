"""Adaptive intent journals around the retained PAPER safety gateway.

This adapter cannot activate an artifact. Source tests use an explicit synthetic
sandbox; a real decision requires durable exact-artifact activation. The existing
cycle still owns recovery, locking, kill switch, data and risk synchronization.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from quantbot.domain import BrokerEnvironment, IntentState, OrderIntent, TradingMode
from quantbot.execution import SubmissionResult
from quantbot.execution.coordinator import persist_broker_acknowledgement
from quantbot.execution.snapshot_capture import capture_account_positions
from quantbot.learner.experiment_contract import DecisionRecord, Digest, ExperimentManifest
from quantbot.learner.experiment_lots import LotEvent, record_lot_event
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.models import LearnerModel, Text
from quantbot.learner.paper import PromotionGatedSubmitter, build_learner_paper_cycle
from quantbot.learner.paper_deadlines import DeadlineGatedSubmitter, _read_record, _signal_id
from quantbot.learner.paper_runner import IntradayPaperServices
from quantbot.operations.cycle import CycleStrategyResult, DataSyncRunner, SubmissionRequest
from quantbot.research.manifest import content_id
from quantbot.risk import DrawdownState, OrderPurpose, PortfolioVetoDecision
from quantbot.storage import StorageRepository
from quantbot.storage.lot_ownership import register_new_lot

if TYPE_CHECKING:
    from quantbot.runtime import Runtime


class ExecutionReceipt(LearnerModel):
    experiment_id: Text
    intent_id: Text
    recorded_at: datetime
    request_hash: Digest
    result: SubmissionResult


def _paper_only(services: IntradayPaperServices) -> None:
    coordinator = services.deadlines.coordinator
    if (
        str(services.runner._broker.environment).upper() != "PAPER"
        or coordinator._broker is not services.runner._broker
        or coordinator._settings.TRADING_MODE is not TradingMode.PAPER
        or coordinator._settings.BROKER_ENVIRONMENT is not BrokerEnvironment.PAPER
        or coordinator._settings.LIVE_TRADING_ACKNOWLEDGED
    ):
        raise ValueError("PAPER_ONLY")


async def prepare_entry(
    manifest: ExperimentManifest,
    decision: DecisionRecord,
    *,
    store: ExperimentStore,
    services: IntradayPaperServices,
    simulation: bool = False,
    alternate_decisions: Sequence[DecisionRecord] = (),
) -> OrderIntent | None:
    manifest = ExperimentManifest.model_validate(manifest.model_dump())
    decision = DecisionRecord.model_validate(decision.model_dump())
    _paper_only(services)
    runner = services.runner
    if (
        not runner.policy.exclusive_learner_entry
        or runner.policy.inherited is None
        or runner.policy.stop_loss_bps > manifest.risk.stop_bps
        or runner.policy.round_trip_cost_bps < manifest.round_trip_cost_bps
        or runner._config.risk_per_trade_bps > manifest.risk.maximum_entry_risk_bps
        or runner._config.max_position_value_bps > manifest.risk.maximum_position_bps
    ):
        raise ValueError("MANIFEST_RISK_MISMATCH")
    state = store.state(manifest.experiment_id)
    if (
        state.manifest_hash != content_id(manifest)
        or decision.experiment_id != manifest.experiment_id
    ):
        raise ValueError("EXPERIMENT_IDENTITY_MISMATCH")
    if simulation:
        if (
            not manifest.synthetic
            or not decision.synthetic
            or str(runner._broker.provider) != "synthetic"
        ):
            raise ValueError("SYNTHETIC_SANDBOX_ONLY")
    elif (
        manifest.synthetic
        or decision.synthetic
        or not decision.paper_eligible
        or state.state != "ACTIVE_PAPER"
        or state.activation_id != decision.activation_id
    ):
        raise ValueError("EXACT_ACTIVATION_REQUIRED")
    now = runner._clock()
    if (
        decision.recorded_at > now
        or decision.model_id != runner.model.model_id
        or decision.action != "BUY"
        or decision.risk_vetoes
        or now < manifest.prospective_start_at
        or decision.features.symbol not in {row.symbol for row in manifest.roster}
    ):
        return None
    matching = [
        prediction
        for prediction in runner.predictions
        if prediction.features == decision.features
        and prediction.model_id == decision.model_id
        and prediction.score == decision.score
        and prediction.recorded_at == decision.recorded_at
    ]
    if len(matching) != 1:
        raise ValueError("EXACT_FORECAST_REQUIRED")
    if decision.score <= manifest.round_trip_cost_bps / 10000:
        return None
    candidates = [decision]
    for alternative in alternate_decisions:
        alternative = DecisionRecord.model_validate(alternative.model_dump())
        if (
            alternative.recorded_at > now
            or alternative.experiment_id != decision.experiment_id
            or alternative.model_id != decision.model_id
            or alternative.activation_id != decision.activation_id
            or alternative.synthetic != decision.synthetic
            or (not simulation and not alternative.paper_eligible)
            or alternative.action != "BUY"
            or alternative.risk_vetoes
            or alternative.score <= manifest.round_trip_cost_bps / 10000
            or alternative.features.symbol not in {row.symbol for row in manifest.roster}
        ):
            raise ValueError("ALTERNATE_DECISION_AUTHORITY_MISMATCH")
        matching_alternative = [
            prediction
            for prediction in runner.predictions
            if prediction.features == alternative.features
            and prediction.model_id == alternative.model_id
            and prediction.score == alternative.score
            and prediction.recorded_at == alternative.recorded_at
        ]
        if len(matching_alternative) != 1:
            raise ValueError("EXACT_ALTERNATE_FORECAST_REQUIRED")
        candidates.append(alternative)
    if len({row.features.symbol for row in candidates}) != len(candidates):
        raise ValueError("DUPLICATE_ENTRY_CANDIDATE")
    # This retained runner evaluates measured sizing, exposure, cost, stop/deadline,
    # pending buys, inherited holdings, calendar, clock, account and promotion gates.
    result = await runner.evaluate()
    if not result.submissions:
        return None
    if len(result.submissions) != 1:
        raise ValueError("ONE_NEW_ENTRY_LIMIT")
    request = result.submissions[0]
    selected = [row for row in candidates if row.features.symbol == request.intent.symbol]
    if len(selected) != 1:
        raise ValueError("RETAINED_GATEWAY_REQUEST_MISMATCH")
    decision = selected[0]
    if (
        request.intent.symbol != decision.features.symbol
        or request.purpose is not OrderPurpose.ENTRY
        or request.risk_approved is not True
        or request.vetoes is None
        or not request.vetoes.allowed
    ):
        raise ValueError("RETAINED_GATEWAY_REQUEST_MISMATCH")
    capture = await capture_account_positions(
        runner._broker,
        clock=runner._clock,
        clock_healthy=getattr(runner, "clock_health", lambda _: False),
    )
    now = runner._clock()
    with runner._database.transaction() as session:
        repository = StorageRepository(session)
        signal = repository.get_signal(_signal_id(request.intent.intent_id))
        if signal is None:
            raise ValueError("PROTECTION_HANDOFF_MISSING")
        record = _read_record(signal)
        if record.entry_intent != request.intent:
            raise ValueError("PROTECTION_HANDOFF_MISMATCH")
        lot_id = register_new_lot(
            session,
            record,
            capture,
            run_id=signal.run_id,
            experiment_id=manifest.experiment_id,
            decision_id=decision.decision_id,
            now=now,
            clock_healthy=bool(getattr(runner, "clock_health", lambda _: False)(now)),
            synthetic=manifest.synthetic,
        )
    record_lot_event(
        store,
        LotEvent(
            experiment_id=manifest.experiment_id,
            lot_id=lot_id,
            decision_id=decision.decision_id,
            symbol=request.intent.symbol,
            account_id_hash=record.account_id_hash,
            recorded_at=now,
            occurred_at=now,
            kind="ENTRY_DECLARED",
            order_id=request.intent.intent_id,
            quantity=0,
            position_before_entry=0,
            source_hash=content_id(capture.evidence()),
            synthetic=manifest.synthetic,
        ),
    )
    store.append(decision, idempotency_key="decision:" + decision.decision_id)
    record_lot_event(
        store,
        LotEvent(
            experiment_id=manifest.experiment_id,
            lot_id=lot_id,
            decision_id=decision.decision_id,
            symbol=request.intent.symbol,
            account_id_hash=record.account_id_hash,
            recorded_at=now,
            occurred_at=now,
            kind="ORDER_LINK",
            order_id=request.intent.intent_id,
            quantity=request.intent.quantity,
            side="BUY",
            owner="ENTRY",
            source_hash=content_id(request.intent),
            synthetic=manifest.synthetic,
        ),
    )
    store.enqueue_intent(
        manifest.experiment_id,
        intent_id=request.intent.intent_id,
        document=request.model_dump_json(),
    )
    return request.intent


class ExperimentOutboxSubmitter:
    """Installed inside the retained cycle, wrapping every existing submission gate."""

    def __init__(
        self,
        store: ExperimentStore,
        *,
        services: IntradayPaperServices,
        lower: PromotionGatedSubmitter,
        owner: str,
        clock: Callable[[], datetime],
    ) -> None:
        _paper_only(services)
        if (
            not isinstance(lower, PromotionGatedSubmitter)
            or not isinstance(lower._submitter, DeadlineGatedSubmitter)
            or lower._submitter._submitter is not services.deadlines.coordinator
            or lower._binding != services.runner.binding
            or lower._identity != services.runner._identity
            or lower._submitter._database is not services.runner._database
            or lower._submitter._account_id != services.runner.account_id
        ):
            raise ValueError("RETAINED_GATEWAY_REQUIRED")
        self.store, self.services, self.lower = store, services, lower
        self.owner, self.clock = owner, clock

    async def submit(
        self,
        intent: OrderIntent,
        *,
        purpose: OrderPurpose,
        market_eligible: bool,
        drawdown: DrawdownState | None,
        risk_approved: bool | None,
        vetoes: PortfolioVetoDecision | None = None,
    ) -> SubmissionResult:
        _paper_only(self.services)
        if purpose is not OrderPurpose.ENTRY:
            raise ValueError("EXPERIMENT_HAS_NO_EXIT_AUTHORITY")
        queued = self.store.get_intent(intent.intent_id)
        if queued is None:
            raise ValueError("DURABLE_OUTBOX_REQUIRED")
        request = SubmissionRequest(
            intent=intent,
            purpose=purpose,
            market_eligible=market_eligible,
            drawdown=drawdown,
            risk_approved=risk_approved,
            vetoes=vetoes,
        )
        if SubmissionRequest.model_validate_json(queued.document) != request:
            raise ValueError("IMMUTABLE_REQUEST_MISMATCH")
        now = self.clock()
        for row in self.store.read_as_of(queued.experiment_id, now):
            if row.kind == "ExecutionReceipt":
                receipt = row.decode(ExecutionReceipt)
                if receipt.intent_id == intent.intent_id and receipt.request_hash == content_id(
                    request
                ):
                    return receipt.result
        claimed = self.store.claim_intent(intent.intent_id, owner=self.owner, now=now)
        if claimed is not None:
            result = await self.lower.submit(
                intent,
                purpose=purpose,
                market_eligible=market_eligible,
                drawdown=drawdown,
                risk_approved=risk_approved,
                vetoes=vetoes,
            )
        else:
            with self.services.runner._database.transaction() as session:
                durable = StorageRepository(session).get_order_intent(intent.intent_id)
            if durable is None:
                raise ValueError("CLAIMED_SEND_REQUIRES_RECOVERY")
            # Read/reconcile only: takeover never releases a claimed ID for a new send.
            result = await _recover_without_send(self.services, durable)
        receipt = ExecutionReceipt(
            experiment_id=queued.experiment_id,
            intent_id=intent.intent_id,
            recorded_at=self.clock(),
            request_hash=content_id(request),
            result=result,
        )
        self.store.append(receipt, idempotency_key="execution-ack:" + intent.intent_id)
        if queued.owner == self.owner or claimed is not None:
            self.store.acknowledge_intent(intent.intent_id, owner=self.owner)
        return result


async def _recover_without_send(
    services: IntradayPaperServices, durable: OrderIntent
) -> SubmissionResult:
    _paper_only(services)
    if durable.state in {IntentState.BROKER_ACCEPTED, IntentState.PARTIALLY_FILLED}:
        return await services.deadlines.coordinator.reconcile_durable(durable)
    if durable.state not in {IntentState.SUBMITTING, IntentState.SUBMISSION_UNKNOWN}:
        raise ValueError("CLAIMED_SEND_REQUIRES_RECOVERY")
    found = await services.runner._broker.get_order_by_client_id(
        durable.client_order_id, expected=durable
    )
    if found is None:
        raise ValueError("CLAIMED_SEND_REQUIRES_RECOVERY")
    with services.runner._database.transaction() as session:
        reconciled = persist_broker_acknowledgement(StorageRepository(session), durable, found)
    return SubmissionResult(intent=reconciled, broker_order=found, recovered_after_ambiguity=True)


async def dispatch_pending(
    store: ExperimentStore, *, services: IntradayPaperServices, now: datetime
) -> tuple[LotEvent, ...]:
    """Periodic recovery can only look up claimed sends; the cycle alone sends entries.

    Broker acknowledgements are ExecutionReceipts, never invented fill events.
    Actual lot-fill reconciliation is a separate read of the durable fill ledger.
    """
    _paper_only(services)
    with store._transaction(write=False) as db:
        experiments = tuple(row[0] for row in db.execute("SELECT experiment_id FROM states"))
    for experiment in experiments:
        for queued in store.pending_intents(experiment):
            if queued.state != "CLAIMED":
                continue
            request = SubmissionRequest.model_validate_json(queued.document)
            if request.intent.strategy_id != services.runner._identity.strategy_id:
                continue
            with services.runner._database.transaction() as session:
                durable = StorageRepository(session).get_order_intent(queued.intent_id)
            if durable is None:
                raise ValueError("CLAIMED_SEND_REQUIRES_RECOVERY")
            receipts = [
                row.decode(ExecutionReceipt)
                for row in store.read_as_of(experiment, now)
                if row.kind == "ExecutionReceipt"
            ]
            if not any(row.intent_id == queued.intent_id for row in receipts):
                result = await _recover_without_send(services, durable)
                store.append(
                    ExecutionReceipt(
                        experiment_id=experiment,
                        intent_id=queued.intent_id,
                        recorded_at=services.runner._clock(),
                        request_hash=content_id(request),
                        result=result,
                    ),
                    idempotency_key="execution-ack:" + queued.intent_id,
                )
            if queued.owner is not None:
                store.acknowledge_intent(queued.intent_id, owner=queued.owner)
    return ()


class ExperimentStrategyRunner:
    """Journal around, rather than replace, the measured-risk entry runner."""

    def __init__(
        self,
        manifest: ExperimentManifest,
        decisions: Sequence[DecisionRecord],
        *,
        store: ExperimentStore,
        services: IntradayPaperServices,
        simulation: bool = False,
    ) -> None:
        self.manifest, self.decisions = manifest, tuple(decisions)
        self.store, self.services, self.simulation = store, services, simulation

    async def evaluate_exits(self) -> CycleStrategyResult:
        return await self.services.runner.evaluate_exits()

    async def evaluate(self) -> CycleStrategyResult:
        eligible = sorted(
            (
                row
                for row in self.decisions
                if row.action == "BUY"
                and not row.risk_vetoes
                and row.score > self.manifest.round_trip_cost_bps / 10000
            ),
            key=lambda row: (-row.score, row.features.symbol),
        )
        if not eligible:
            return CycleStrategyResult(submissions=(), signal_count=len(self.decisions))
        intent = await prepare_entry(
            self.manifest,
            eligible[0],
            store=self.store,
            services=self.services,
            simulation=self.simulation,
            alternate_decisions=eligible[1:],
        )
        queued = self.store.get_intent(intent.intent_id) if intent is not None else None
        return CycleStrategyResult(
            submissions=()
            if queued is None
            else (SubmissionRequest.model_validate_json(queued.document),),
            signal_count=len(self.decisions),
        )


def wire_experiment_services(
    runtime: Runtime,
    services: IntradayPaperServices,
    *,
    manifest: ExperimentManifest,
    decisions: Sequence[DecisionRecord],
    store: ExperimentStore,
    owner: str,
    data_sync: DataSyncRunner,
    clock: Callable[[], datetime],
    simulation: bool = False,
) -> IntradayPaperServices:
    """Retain the complete operational lifecycle and every lower submission gate.

    No deployment, activation, daemon start, schema update or kill-switch mutation.
    The caller binds the actual run ID to services.runner before calling cycle.run.
    """
    strategy = ExperimentStrategyRunner(
        manifest, decisions, store=store, services=services, simulation=simulation
    )
    cycle = build_learner_paper_cycle(
        runtime,
        strategy,
        binding=services.runner.binding,
        identity=services.runner._identity,
        data_sync=data_sync,
        max_data_age_seconds=services.runner.policy.max_reference_age_seconds,
        clock=clock,
        submission_wrapper=lambda lower: ExperimentOutboxSubmitter(
            store, services=services, lower=lower, owner=owner, clock=clock
        ),
    )
    return IntradayPaperServices(runner=services.runner, cycle=cycle, deadlines=services.deadlines)
