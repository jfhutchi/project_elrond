"""Independent PAPER holding deadlines, durably handed off before order submission.

No predictor, promotion gate, entry kill switch, or market-data refresh controls this
supervisor. It verifies account, order and fill provenance and uses the existing
coordinator's risk-reducing exit authority. It complements position protection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator
from sqlalchemy import select

from quantbot.brokers import BrokerAdapter
from quantbot.domain import (
    BrokerEnvironment,
    BrokerOrder,
    Fill,
    IntentState,
    OrderIntent,
    OrderSide,
    OrderType,
    StrategyIdentity,
    TimeInForce,
    TradingMode,
)
from quantbot.execution import ExecutionCoordinator, SubmissionResult
from quantbot.execution.coordinator import persist_broker_acknowledgement
from quantbot.execution.recovery import is_open_order
from quantbot.learner.models import LearnerModel
from quantbot.learner.paper import PaperModelBinding
from quantbot.operations.cycle import OrderSubmitter
from quantbot.research.manifest import content_id
from quantbot.risk import DrawdownState, OrderPurpose, PortfolioVetoDecision
from quantbot.storage import Database, SingleWriterLock, StorageRepository
from quantbot.storage.lock import AlreadyLockedError
from quantbot.storage.repositories import SignalRecord
from quantbot.storage.schema import order_intents

HANDOFF = "LEARNER_PAPER_DEADLINE_V1"
HEARTBEAT = "LEARNER_PAPER_DEADLINE_HEARTBEAT_V1"
EXIT_EVENT = "LEARNER_PAPER_DEADLINE_EXIT_V1"
CLOSED_EVENT = "LEARNER_PAPER_DEADLINE_CLOSED_V1"
HEARTBEAT_SECONDS = 60
MAX_EXIT_ATTEMPTS = 3


def _now() -> datetime:
    return datetime.now(UTC)


class PaperDeadline(LearnerModel):
    entry_intent: OrderIntent
    binding: PaperModelBinding
    identity: StrategyIdentity
    model_id: str = Field(min_length=1)
    prediction_id: str = Field(min_length=1)
    account_id_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    recorded_at: datetime
    reference_at: datetime
    entry_valid_until: datetime
    deadline_at: datetime
    session_exit_at: datetime
    initial_stop: Decimal = Field(gt=0, allow_inf_nan=False)

    @staticmethod
    def account_hash(account_id: str) -> str:
        return content_id({"paper_account": account_id})

    @model_validator(mode="after")
    def validate_handoff(self) -> Self:
        expected = self.binding.strategy_identity(
            git_commit=self.identity.git_commit, deployed_at=self.identity.deployment_timestamp
        )
        if self.identity != expected or self.model_id != self.binding.model_id:
            raise ValueError("deadline model and deployment binding disagree")
        intent = self.entry_intent
        if (
            intent.strategy_id != self.identity.strategy_id
            or intent.side is not OrderSide.BUY
            or intent.state is not IntentState.RISK_APPROVED
            or intent.order_type is not OrderType.MARKET
            or intent.time_in_force is not TimeInForce.DAY
            or intent.extended_hours
        ):
            raise ValueError("deadline must bind the exact new long PAPER entry")
        if not intent.created_at <= self.recorded_at < self.deadline_at:
            raise ValueError("deadline must be handed off before it becomes due")
        if not (
            self.reference_at
            <= self.recorded_at
            < self.entry_valid_until
            <= self.reference_at + timedelta(seconds=120)
        ):
            raise ValueError("entry reference must expire within two minutes")
        if self.deadline_at > intent.created_at + timedelta(minutes=30):
            raise ValueError("holding deadline cannot exceed thirty minutes")
        if self.deadline_at > self.session_exit_at:
            raise ValueError("holding deadline cannot exceed the session exit")
        return self

    @property
    def handoff_id(self) -> str:
        return content_id(self)


def _signal_id(intent_id: str) -> str:
    return "paper-deadline-" + content_id({"intent_id": intent_id})


def record_deadline(database: Database, record: PaperDeadline, *, run_id: str) -> bool:
    record = PaperDeadline.model_validate(record.model_dump())
    with database.transaction() as session:
        repository = StorageRepository(session)
        deployment = repository.get_strategy_deployment(
            record.identity.strategy_id, record.identity.version
        )
        run = repository.get_run(run_id)
        if (
            deployment != record.identity
            or run is None
            or run.strategy_id != record.identity.strategy_id
        ):
            raise ValueError("deadline handoff requires the matching deployed strategy and cycle")
        return repository.record_signal(
            _signal_id(record.entry_intent.intent_id),
            run_id=run_id,
            strategy_id=record.identity.strategy_id,
            symbol=record.entry_intent.symbol,
            occurred_at=record.recorded_at,
            payload={
                "kind": HANDOFF,
                "document": record.model_dump(mode="json"),
                "document_hash": record.handoff_id,
            },
        )


def _read_record(signal: SignalRecord) -> PaperDeadline:
    record = PaperDeadline.model_validate(signal.payload.get("document"))
    if (
        signal.payload.get("kind") != HANDOFF
        or signal.payload.get("document_hash") != record.handoff_id
        or signal.signal_id != _signal_id(record.entry_intent.intent_id)
        or signal.strategy_id != record.identity.strategy_id
        or signal.symbol != record.entry_intent.symbol
        or signal.occurred_at != record.recorded_at
    ):
        raise ValueError("deadline handoff integrity mismatch")
    return record


def supervisor_objections(database: Database, *, account_id: str, now: datetime) -> tuple[str, ...]:
    account_hash = PaperDeadline.account_hash(account_id)
    with database.transaction() as session:
        repository = StorageRepository(session)
        signals = [
            row
            for row in repository.list_signals(symbol="PAPER_DEADLINES")
            if row.payload.get("kind") == HEARTBEAT
            and row.payload.get("account_id_hash") == account_hash
        ]
        if not signals:
            return ("DEADLINE_SUPERVISOR_UNAVAILABLE",)
        heartbeat = signals[-1]
        run = repository.get_run(heartbeat.run_id)
        if run is None or run.status != "SUCCEEDED" or heartbeat.payload.get("healthy") is not True:
            return ("DEADLINE_SUPERVISOR_UNHEALTHY",)
    age = (now - heartbeat.occurred_at).total_seconds()
    return () if 0 <= age <= HEARTBEAT_SECONDS else ("DEADLINE_SUPERVISOR_STALE",)


def deadline_entry_objections(
    database: Database,
    intent: OrderIntent,
    *,
    account_id: str,
    now: datetime,
) -> tuple[str, ...]:
    with database.transaction() as session:
        repository = StorageRepository(session)
        signal = repository.get_signal(_signal_id(intent.intent_id))
        if signal is None:
            return ("DEADLINE_HANDOFF_MISSING",)
        record = _read_record(signal)
        deployment = repository.get_strategy_deployment(
            record.identity.strategy_id, record.identity.version
        )
    reasons = []
    if deployment != record.identity or record.entry_intent != intent:
        reasons.append("DEADLINE_INTENT_MISMATCH")
    if record.account_id_hash != PaperDeadline.account_hash(account_id):
        reasons.append("DEADLINE_ACCOUNT_MISMATCH")
    if not record.recorded_at <= now < min(record.deadline_at, record.session_exit_at):
        reasons.append("DEADLINE_ALREADY_DUE")
    if now >= record.entry_valid_until:
        reasons.append("PAPER_ENTRY_REFERENCE_EXPIRED")
    return tuple(reasons) + supervisor_objections(database, account_id=account_id, now=now)


class DeadlineEntryBlocked(ValueError):
    """Known rejection before the durable coordinator or broker is invoked."""

    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons = reasons
        super().__init__(", ".join(reasons))


class DeadlineGatedSubmitter:
    def __init__(
        self,
        database: Database,
        submitter: OrderSubmitter,
        *,
        expected_account_id: str,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._database, self._submitter = database, submitter
        self._account_id, self._clock = expected_account_id, clock

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
        if purpose is OrderPurpose.ENTRY:
            reasons = deadline_entry_objections(
                self._database, intent, account_id=self._account_id, now=self._clock()
            )
            if reasons:
                raise DeadlineEntryBlocked(reasons)
        return await self._submitter.submit(
            intent,
            purpose=purpose,
            market_eligible=market_eligible,
            drawdown=drawdown,
            risk_approved=risk_approved,
            vetoes=vetoes,
        )


class DeadlineResult(LearnerModel):
    reasons: tuple[str, ...]
    exits_submitted: int = Field(ge=0)

    @property
    def healthy(self) -> bool:
        return not self.reasons


class PaperDeadlineSupervisor:
    def __init__(
        self,
        *,
        database: Database,
        broker: BrokerAdapter,
        coordinator: ExecutionCoordinator,
        identity: StrategyIdentity,
        expected_account_id: str,
        lock_path: str | Path,
        clock: Callable[[], datetime] = _now,
        reconcile_fills: Callable[[datetime], Awaitable[tuple[str, ...]]] | None = None,
    ) -> None:
        if not isinstance(coordinator, ExecutionCoordinator):
            raise TypeError("deadline exits require ExecutionCoordinator")
        if (
            coordinator._settings.TRADING_MODE is not TradingMode.PAPER
            or coordinator._settings.BROKER_ENVIRONMENT is not BrokerEnvironment.PAPER
            or coordinator._settings.EXPECTED_ACCOUNT_ID != expected_account_id
            or coordinator._database is not database
            or coordinator._broker is not broker
        ):
            raise ValueError(
                "deadline supervisor requires the same PAPER coordinator/account/store"
            )
        if not expected_account_id.strip():
            raise ValueError("deadline supervisor requires a verified account identity")
        self.database, self.broker, self.coordinator = database, broker, coordinator
        self.identity, self.account_id = identity, expected_account_id
        self.lock_path, self.clock = lock_path, clock
        self.reconcile_fills = reconcile_fills

    async def run_once(self) -> DeadlineResult:
        now = self.clock()
        stamp = content_id({"account": self.account_id, "at": now, "kind": HEARTBEAT})
        run_id = "paper-deadline-" + stamp
        with SingleWriterLock(self.lock_path):
            with self.database.transaction() as session:
                repository = StorageRepository(session)
                existing = repository.get_run(run_id)
                if existing is not None:
                    raise ValueError("deadline supervisor clock must advance between passes")
                if (
                    repository.get_strategy_deployment(
                        self.identity.strategy_id, self.identity.version
                    )
                    != self.identity
                ):
                    raise ValueError("deadline audit identity must already be deployed")
                repository.create_run(
                    run_id,
                    self.identity,
                    started_at=now,
                    detail={"role": "PAPER_DEADLINE_SUPERVISOR"},
                )
                records = [
                    row for row in repository.list_signals() if row.payload.get("kind") == HANDOFF
                ]
            reasons: list[str] = []
            failed_handoffs: list[str] = []
            submitted = 0
            try:
                account = await self.broker.get_account()
                market = await self.broker.get_market_clock()
                measured_at = self.clock()
                if measured_at < now:
                    reasons.append("DEADLINE_LOCAL_CLOCK_REVERSED")
                elif account.account.account_id != self.account_id:
                    reasons.append("DEADLINE_ACCOUNT_MISMATCH")
                elif not 0 <= (measured_at - market.timestamp).total_seconds() <= 120:
                    reasons.append("DEADLINE_BROKER_CLOCK_STALE")
                else:
                    now = measured_at
                    for row in records:
                        try:
                            record = _read_record(row)
                            if record.account_id_hash != PaperDeadline.account_hash(
                                self.account_id
                            ):
                                continue
                            submitted += await self._process(
                                record, now=now, market_open=market.is_open
                            )
                        except Exception as error:
                            reason = (
                                str(error)
                                if isinstance(error, DeadlineStateError)
                                else "DEADLINE_BROKER_OR_LEDGER_UNAVAILABLE"
                            )
                            reasons.append(reason)
                            failed_handoffs.append(row.signal_id)
            except Exception:
                reasons.append("DEADLINE_BROKER_UNAVAILABLE")
            # Existing exits have priority. Account refresh cannot submit orders,
            # clear a stop, or make a completed exit disappear on retry.
            if self.reconcile_fills is not None and not reasons:
                try:
                    await self._reconcile_confirmed_fills()
                except Exception:
                    reasons.append("DEADLINE_POST_FILL_RECONCILIATION_REQUIRED")
            result = DeadlineResult(
                reasons=tuple(dict.fromkeys(reasons)), exits_submitted=submitted
            )
            with self.database.transaction() as session:
                repository = StorageRepository(session)
                repository.record_signal(
                    "paper-heartbeat-" + stamp,
                    run_id=run_id,
                    strategy_id=self.identity.strategy_id,
                    symbol="PAPER_DEADLINES",
                    occurred_at=now,
                    payload={
                        "kind": HEARTBEAT,
                        "account_id_hash": PaperDeadline.account_hash(self.account_id),
                        "healthy": result.healthy,
                        "reasons": result.reasons,
                    },
                )
                repository.finish_run(
                    run_id,
                    finished_at=self.clock(),
                    status="SUCCEEDED" if result.healthy else "ERROR",
                    detail=result.model_dump(mode="json"),
                )
                if not result.healthy:
                    failure_fingerprint = content_id({
                        "account_id_hash": PaperDeadline.account_hash(self.account_id),
                        "strategy_id": self.identity.strategy_id,
                        "configuration_hash": self.identity.configuration_hash,
                        "handoffs": sorted(failed_handoffs or [row.signal_id for row in records]),
                        "reasons": sorted(result.reasons),
                    })
                    # Every failed run and heartbeat remains durable. Deduplicate
                    # only the exact still-open incident; never resolve or hide it.
                    if not any(
                        incident.kind == "PAPER_DEADLINE_FAILURE"
                        and incident.detail.get("failure_fingerprint") == failure_fingerprint
                        for incident in repository.list_incidents(unresolved_only=True)
                    ):
                        repository.create_incident(
                            "paper-deadline-incident-" + stamp,
                            severity="ERROR",
                            kind="PAPER_DEADLINE_FAILURE",
                            message="Independent learner deadline protection needs attention",
                            occurred_at=now,
                            run_id=run_id,
                            detail={"reasons": result.reasons,
                                    "failure_fingerprint": failure_fingerprint},
                        )
            return result

    async def serve(self, stop: asyncio.Event, *, interval_seconds: float = 10) -> None:
        if not 0 < interval_seconds <= HEARTBEAT_SECONDS / 2:
            raise ValueError("deadline polling must be faster than heartbeat expiry")
        while not stop.is_set():
            try:
                await self.run_once()
            except AlreadyLockedError:
                # Another legitimate cycle owns the same writer lock. Never bypass
                # it or issue a fictional heartbeat; try again at the next poll.
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass

    async def _sync_order(self, intent: OrderIntent) -> tuple[BrokerOrder | None, tuple[Fill, ...]]:
        order = await self.broker.get_order_by_client_id(intent.client_order_id, expected=intent)
        if order is None:
            return None, ()
        if (
            order.client_order_id,
            order.symbol,
            order.side,
            order.quantity,
            order.order_type,
            order.time_in_force,
        ) != (
            intent.client_order_id,
            intent.symbol,
            intent.side,
            intent.quantity,
            intent.order_type,
            intent.time_in_force,
        ):
            raise DeadlineStateError("DEADLINE_BROKER_INTENT_MISMATCH")
        # Preserve the entry chronology floor. Owned risk-reducing exits use
        # their provider's own submission clock, not a different local clock.
        fill_not_before = order.submitted_at if intent.side is OrderSide.SELL else intent.created_at
        fills = tuple(
            fill
            for fill in await self.broker.get_fills(after=fill_not_before)
            if fill.broker_order_id == order.broker_order_id
        )
        measured_at = self.clock()
        if (
            order.submitted_at > measured_at
            or len({fill.fill_id for fill in fills}) != len(fills)
            or any(
                fill.symbol != intent.symbol
                or fill.side is not intent.side
                or not fill_not_before <= fill.occurred_at <= measured_at
                for fill in fills
            )
            or sum((fill.quantity for fill in fills), Decimal(0)) != order.filled_quantity
        ):
            raise DeadlineStateError("DEADLINE_FILL_LEDGER_MISMATCH")
        with self.database.transaction() as session:
            repository = StorageRepository(session)
            stored = repository.get_order_intent(intent.intent_id)
            if stored is None:
                raise DeadlineStateError("DEADLINE_INTENT_MISSING")
            known = repository.get_broker_order_by_client_order_id(intent.client_order_id)
            if known is None:
                persist_broker_acknowledgement(repository, stored, order)
            else:
                if known.broker_order_id != order.broker_order_id:
                    raise DeadlineStateError("DEADLINE_BROKER_INTENT_MISMATCH")
                repository.update_broker_order_lifecycle(order)
                target = (
                    IntentState.FILLED
                    if order.filled_quantity == order.quantity
                    else IntentState.CANCELLED
                    if order.status in {"canceled", "cancelled"}
                    else IntentState.REJECTED
                    if order.status == "rejected"
                    else IntentState.EXPIRED
                    if order.status == "expired"
                    else IntentState.PARTIALLY_FILLED
                    if order.filled_quantity
                    else None
                )
                if target is not None and stored.state not in {
                    IntentState.FILLED,
                    IntentState.CANCELLED,
                    IntentState.REJECTED,
                    IntentState.EXPIRED,
                    IntentState.ERROR,
                }:
                    repository.transition_order_intent(
                        stored.intent_id, target, expected_current=stored.state
                    )
            for fill in fills:
                repository.record_fill(fill)
            if fills and self.reconcile_fills is not None:
                fingerprint = content_id({"account": PaperDeadline.account_hash(self.account_id),
                                          "intent": intent.intent_id,
                                          "fills": sorted(fill.fill_id for fill in fills)})
                repository.record_order_event(
                    "fill-reconciliation-request-" + fingerprint,
                    event_type="PAPER_FILL_RECONCILIATION_REQUEST_V1",
                    occurred_at=max(fill.occurred_at for fill in fills),
                    intent_id=intent.intent_id, broker_order_id=order.broker_order_id,
                    detail={"account_id_hash": PaperDeadline.account_hash(self.account_id),
                            "fill_evidence_hash": content_id(tuple(sorted(
                                (fill.model_dump(mode="json") for fill in fills),
                                key=lambda row: str(row["fill_id"]))))},
                )
        return order, fills

    async def _reconcile_confirmed_fills(self) -> None:
        with self.database.transaction() as session:
            repository = StorageRepository(session)
            pending = [event for event in repository.list_order_events()
                       if event.event_type == "PAPER_FILL_RECONCILIATION_REQUEST_V1"
                       and event.detail.get("account_id_hash") == PaperDeadline.account_hash(
                           self.account_id)
                       and repository.get_order_event("fill-reconciled-" + event.event_id)
                       is None]
        if not pending:
            return
        assert self.reconcile_fills is not None
        reasons = await asyncio.wait_for(self.reconcile_fills(self.clock()), timeout=5)
        if reasons:
            raise DeadlineStateError("DEADLINE_POST_FILL_RECONCILIATION_REQUIRED")
        with self.database.transaction() as session:
            repository = StorageRepository(session)
            for event in pending:
                repository.record_order_event(
                    "fill-reconciled-" + event.event_id,
                    event_type="PAPER_FILL_RECONCILED_V1", occurred_at=self.clock(),
                    intent_id=event.intent_id, broker_order_id=event.broker_order_id,
                    detail={"request_event_id": event.event_id,
                            "entry_authority_granted": False, "kill_switch_cleared": False},
                )

    async def _process(self, record: PaperDeadline, *, now: datetime, market_open: bool) -> int:
        with self.database.transaction() as session:
            repository = StorageRepository(session)
            if repository.get_order_event("deadline-closed-" + record.handoff_id) is not None:
                return 0
            if (
                repository.get_strategy_deployment(
                    record.identity.strategy_id, record.identity.version
                )
                != record.identity
            ):
                raise DeadlineStateError("DEADLINE_DEPLOYMENT_MISMATCH")
            entry = repository.get_order_intent(record.entry_intent.intent_id)
            if entry is None:  # Durable handoff preceding an entry that was never submitted.
                return 0
            if entry.model_copy(update={"state": IntentState.RISK_APPROVED}) != record.entry_intent:
                raise DeadlineStateError("DEADLINE_INTENT_MISMATCH")
            events = [
                event
                for event in repository.list_order_events()
                if event.event_type == EXIT_EVENT
                and event.detail.get("handoff_id") == record.handoff_id
            ]
            exits = [
                repository.get_order_intent(event.intent_id) if event.intent_id else None
                for event in events
            ]
            owned_ids = {item.intent_id for item in exits if item is not None}
            external_ids = (
                session.execute(
                    select(order_intents.c.intent_id).where(
                        order_intents.c.symbol == entry.symbol,
                        order_intents.c.side == OrderSide.SELL.value,
                    )
                )
                .scalars()
                .all()
            )
            external = [
                repository.get_order_intent(str(identifier))
                for identifier in external_ids
                if identifier not in owned_ids
            ]
            unresolved_external = [
                item
                for item in repository.list_unresolved_order_intents()
                if item.symbol == entry.symbol
                and item.side is OrderSide.SELL
                and item.intent_id not in owned_ids
            ]
        entry_order, bought = await self._sync_order(entry)
        if entry_order is None:
            if entry.state in {
                IntentState.ERROR,
                IntentState.CANCELLED,
                IntentState.REJECTED,
                IntentState.EXPIRED,
                IntentState.RISK_APPROVED,
                IntentState.ORDER_CREATED,
            }:
                return 0
            raise DeadlineStateError("DEADLINE_ENTRY_SUBMISSION_UNKNOWN")
        positions = [
            position
            for position in await self.broker.get_positions()
            if position.symbol == entry.symbol and position.quantity != 0
        ]
        due = now >= min(record.deadline_at, record.session_exit_at)
        due = due or bool(positions and positions[0].market_price <= record.initial_stop)
        # Stop an overdue entry from accumulating more exposure even when another
        # protection authority already owns a pending exit for its partial fill.
        if due and is_open_order(entry_order):
            await self.broker.cancel_order(entry_order.broker_order_id)
            entry_order, bought = await self._sync_order(entry)
            if entry_order is None or is_open_order(entry_order):
                raise DeadlineStateError("DEADLINE_ENTRY_CANCEL_UNCONFIRMED")
            positions = [
                position
                for position in await self.broker.get_positions()
                if position.symbol == entry.symbol and position.quantity != 0
            ]
        owned_clients = {item.client_order_id for item in exits if item is not None}
        if any(
            order.symbol == entry.symbol
            and order.side is OrderSide.SELL
            and order.client_order_id not in owned_clients
            for order in await self.broker.get_open_orders()
        ):
            raise DeadlineStateError("DEADLINE_EXTERNAL_EXIT_PENDING")
        for pending_exit in unresolved_external:
            confirmed, _ = await self._sync_order(pending_exit)
            if confirmed is None or is_open_order(confirmed):
                raise DeadlineStateError("DEADLINE_EXTERNAL_EXIT_PENDING")
        sold: list[Fill] = []
        working_exit: OrderIntent | None = None
        for exit_intent in exits:
            if exit_intent is None:
                raise DeadlineStateError("DEADLINE_EXIT_INTENT_MISSING")
            order, fills = await self._sync_order(exit_intent)
            sold.extend(fills)
            if order is None or is_open_order(order):
                working_exit = exit_intent
        for external_intent in external:
            if external_intent is None or external_intent.created_at < entry.created_at:
                continue
            order, external_fills = await self._sync_order(external_intent)
            if order is None or is_open_order(order):
                raise DeadlineStateError("DEADLINE_EXTERNAL_EXIT_UNVERIFIED")
            # A terminal protective exit from the existing supervisor can reduce this
            # holding. Count only its independently verified, durable broker fills.
            sold.extend(external_fills)
        remaining = sum((fill.quantity for fill in bought), Decimal(0)) - sum(
            (fill.quantity for fill in sold), Decimal(0)
        )
        if remaining < 0:
            raise DeadlineStateError("DEADLINE_EXIT_QUANTITY_MISMATCH")
        if remaining == 0 and not is_open_order(entry_order):
            with self.database.transaction() as session:
                StorageRepository(session).record_order_event(
                    "deadline-closed-" + record.handoff_id,
                    event_type=CLOSED_EVENT,
                    occurred_at=now,
                    intent_id=entry.intent_id,
                    detail={"handoff_id": record.handoff_id},
                )
            return 0
        if not due:
            return 0
        if remaining == 0:
            return 0
        if len(positions) != 1 or positions[0].quantity != remaining:
            raise DeadlineStateError("DEADLINE_POSITION_MISMATCH")
        if not market_open:
            raise DeadlineStateError("DEADLINE_MARKET_CLOSED")
        if working_exit is not None:
            # Recovery uses the SAME durable identity; no repeated or replacement sell.
            await self.coordinator.submit_durable(
                working_exit.model_copy(update={"state": IntentState.RISK_APPROVED}),
                purpose=OrderPurpose.STRATEGY_EXIT,
                market_eligible=True,
                drawdown=None,
                risk_approved=None,
                vetoes=None,
            )
            return 0
        if len(exits) >= MAX_EXIT_ATTEMPTS:
            raise DeadlineStateError("DEADLINE_EXIT_RETRIES_EXHAUSTED")
        digest = content_id({"handoff": record.handoff_id, "attempt": len(exits)})
        exit_intent = OrderIntent(
            intent_id="deadline-exit-" + digest,
            client_order_id="qb-" + digest[:40],
            strategy_id=entry.strategy_id,
            signal_date=now.date(),
            symbol=entry.symbol,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            quantity=remaining,
            created_at=now,
        )
        with self.database.transaction() as session:
            repository = StorageRepository(session)
            repository.create_order_intent(exit_intent)
            repository.record_order_event(
                "deadline-exit-event-" + digest,
                event_type=EXIT_EVENT,
                occurred_at=now,
                intent_id=exit_intent.intent_id,
                detail={"handoff_id": record.handoff_id, "attempt": len(exits)},
            )
        await self.coordinator.submit_durable(
            exit_intent,
            purpose=OrderPurpose.STRATEGY_EXIT,
            market_eligible=True,
            drawdown=None,
            risk_approved=None,
            vetoes=None,
        )
        return 1


class DeadlineStateError(RuntimeError):
    """A fixed, nonsecret reason suitable for the durable incident ledger."""
