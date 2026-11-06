"""Durable order submission with bounded same-identity ambiguity recovery."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from pydantic import Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantbot.brokers import (
    BrokerAccountSnapshot,
    BrokerAdapter,
    BrokerProtocolError,
    BrokerRequestError,
    BrokerTransportError,
    SubmissionUncertainError,
)
from quantbot.brokers.base import BrokerModel
from quantbot.config import Settings
from quantbot.domain import (
    BrokerOrder,
    IntentState,
    InvalidOrderTransition,
    OrderIntent,
    OrderSide,
    Position,
    ReconciliationStatus,
)
from quantbot.risk import (
    DrawdownState,
    OrderGateDecision,
    OrderPurpose,
    PortfolioVetoDecision,
    evaluate_order_gate,
)
from quantbot.storage import Database, StateConflictError, StorageRepository


class SubmissionPolicy(BrokerModel):
    max_not_found_retries: int = Field(default=1, ge=0, le=3)


class SubmissionResult(BrokerModel):
    intent: OrderIntent
    broker_order: BrokerOrder
    recovered_after_ambiguity: bool = False
    not_found_retries: int = 0


class OrderSubmissionBlocked(RuntimeError):
    """Raised before broker mutation when any operational gate fails."""

    def __init__(self, decision: OrderGateDecision) -> None:
        self.decision = decision
        super().__init__("order submission blocked: " + ", ".join(decision.reasons))


class OrderIntentNotResumable(RuntimeError):
    """Raised when a durable intent has already reached an incompatible terminal state."""

    def __init__(self, intent: OrderIntent) -> None:
        self.intent = intent
        super().__init__(f"order intent {intent.intent_id} cannot resume from {intent.state.value}")


def _assert_ack_matches_intent(intent: OrderIntent, order: BrokerOrder) -> None:
    if (
        order.client_order_id != intent.client_order_id
        or order.symbol != intent.symbol
        or order.side is not intent.side
        or order.order_type is not intent.order_type
        or order.time_in_force is not intent.time_in_force
        or order.quantity != intent.quantity
    ):
        raise BrokerProtocolError("broker acknowledgement does not match durable intent")


def _ack_terminal_target(order: BrokerOrder) -> IntentState | None:
    status = order.status.strip().lower()
    if status in {"rejected"}:
        return IntentState.REJECTED
    if order.filled_quantity == order.quantity or status == "filled":
        return IntentState.FILLED
    if status in {"canceled", "cancelled"}:
        return IntentState.CANCELLED
    if status == "expired":
        return IntentState.EXPIRED
    if order.filled_quantity > 0 or status in {"partially_filled", "partial_fill"}:
        return IntentState.PARTIALLY_FILLED
    return None


def persist_broker_acknowledgement(
    repository: StorageRepository,
    intent: OrderIntent,
    order: BrokerOrder,
) -> OrderIntent:
    """Adopt one broker acknowledgement in the caller-owned transaction."""
    _assert_ack_matches_intent(intent, order)
    repository.save_broker_order(order)
    terminal_target = _ack_terminal_target(order)
    if terminal_target is IntentState.REJECTED:
        return repository.transition_order_intent(
            intent.intent_id,
            IntentState.REJECTED,
            expected_current=intent.state,
        )
    accepted = repository.transition_order_intent(
        intent.intent_id,
        IntentState.BROKER_ACCEPTED,
        expected_current=intent.state,
    )
    if terminal_target is None:
        return accepted
    return repository.transition_order_intent(
        intent.intent_id,
        terminal_target,
        expected_current=IntentState.BROKER_ACCEPTED,
    )


def _risk_reducing_exit_reasons(
    intent: OrderIntent,
    positions: tuple[Position, ...],
) -> tuple[str, ...]:
    """Prove an exit reduces the fresh broker position rather than trusting its purpose label."""
    matching = tuple(position for position in positions if position.symbol == intent.symbol)
    if not matching:
        return ("EXIT_POSITION_NOT_FOUND",)
    if len(matching) != 1:
        return ("EXIT_POSITION_AMBIGUOUS",)

    position = matching[0]
    if position.quantity == 0:
        return ("EXIT_POSITION_NOT_FOUND",)
    reducing_side = OrderSide.SELL if position.quantity > 0 else OrderSide.BUY
    reasons: list[str] = []
    if intent.side is not reducing_side:
        reasons.append("EXIT_SIDE_INCREASES_RISK")
    if intent.quantity > abs(position.quantity):
        reasons.append("EXIT_QUANTITY_EXCEEDS_POSITION")
    return tuple(reasons)


def _competing_exit(pending: OrderIntent, intent: OrderIntent) -> bool:
    if (
        pending.symbol != intent.symbol
        or pending.side is not intent.side
        or pending.intent_id == intent.intent_id
    ):
        return False
    if pending.state is IntentState.RISK_APPROVED:
        # A claimed order has precedence over every merely prepared request.
        # Before claiming, deterministic priority lets one of two prepared exits
        # proceed without deleting the other authority's durable evidence.
        return intent.state is IntentState.RISK_APPROVED and (
            pending.created_at,
            pending.intent_id,
        ) < (intent.created_at, intent.intent_id)
    return True


class ExecutionCoordinator:
    """Persist before network I/O and never replace a client order identity."""

    def __init__(
        self,
        database: Database,
        broker: BrokerAdapter,
        settings: Settings,
        *,
        submission_policy: SubmissionPolicy | None = None,
        final_send_check: Callable[[OrderIntent, OrderPurpose], tuple[str, ...]] | None = None,
        transactional_send_check: Callable[[Session, OrderIntent, OrderPurpose], tuple[str, ...]]
        | None = None,
        transactional_send_refusal: Callable[
            [Session, OrderIntent, OrderPurpose, tuple[str, ...]], None
        ]
        | None = None,
    ) -> None:
        self._database = database
        self._broker = broker
        self._settings = settings
        self._submission_policy = submission_policy or SubmissionPolicy()
        self._final_send_check = final_send_check
        self._transactional_send_check = transactional_send_check
        self._transactional_send_refusal = transactional_send_refusal

    def _gate(
        self,
        account_snapshot: BrokerAccountSnapshot,
        positions: tuple[Position, ...],
        intent: OrderIntent,
        purpose: OrderPurpose,
        market_eligible: bool,
        drawdown: DrawdownState | None,
        risk_approved: bool | None,
        vetoes: PortfolioVetoDecision | None,
        open_orders: tuple[BrokerOrder, ...],
    ) -> OrderGateDecision:
        with self._database.transaction() as session:
            repository = StorageRepository(session)
            durable_kill = repository.get_kill_switch_state().engaged
            latest_reconciliation = repository.get_latest_reconciliation()
            competing_exit = purpose in {
                OrderPurpose.STRATEGY_EXIT,
                OrderPurpose.DRAWDOWN_LIQUIDATION,
            } and any(
                _competing_exit(pending, intent)
                for pending in repository.list_unresolved_order_intents()
            )
        durable_reconciled = (
            latest_reconciliation is not None
            and latest_reconciliation.status is ReconciliationStatus.RECONCILED
            and not latest_reconciliation.diffs
        )
        effective_settings = self._settings.model_copy(
            update={
                "RECONCILIATION_SUCCESSFUL": (
                    self._settings.RECONCILIATION_SUCCESSFUL and durable_reconciled
                )
            }
        )
        decision = evaluate_order_gate(
            effective_settings,
            account_snapshot.account.account_id,
            purpose,
            durable_kill,
            market_eligible,
            drawdown=drawdown,
            risk_approved=risk_approved,
            vetoes=vetoes,
        )
        reasons = list(decision.reasons)
        if purpose in {
            OrderPurpose.STRATEGY_EXIT,
            OrderPurpose.DRAWDOWN_LIQUIDATION,
        }:
            reasons.extend(_risk_reducing_exit_reasons(intent, positions))
            if competing_exit or any(
                order.symbol == intent.symbol
                and order.side is intent.side
                and order.client_order_id != intent.client_order_id
                for order in open_orders
            ):
                reasons.append("EXIT_REDUCING_ORDER_PENDING")
        if not account_snapshot.order_entry_allowed:
            reasons.append("BROKER_ORDER_ENTRY_DISABLED")
        if account_snapshot.restrictions:
            reasons.append("BROKER_ACCOUNT_RESTRICTED")
        return decision.model_copy(
            update={"allowed": not reasons, "reasons": tuple(dict.fromkeys(reasons))}
        )

    async def _open_exit_orders(self, purpose: OrderPurpose) -> tuple[BrokerOrder, ...]:
        if purpose in {OrderPurpose.STRATEGY_EXIT, OrderPurpose.DRAWDOWN_LIQUIDATION}:
            return tuple(await self._broker.get_open_orders())
        return ()

    def _claim_exit(self, intent: OrderIntent, decision: OrderGateDecision) -> OrderIntent:
        """Atomically reserve the reducing side using the existing durable intent state."""
        if self._database.engine.dialect.name != "sqlite":
            raise OrderSubmissionBlocked(
                decision.model_copy(
                    update={"allowed": False, "reasons": ("EXIT_ATOMIC_CLAIM_UNSUPPORTED",)}
                )
            )
        with self._database.transaction() as session:
            # Both protection daemons may have different process locks. Reserve the
            # SQLite writer before inspecting contenders, then commit ORDER_CREATED
            # in the same transaction; no broker I/O happens while holding this lock.
            session.execute(text("BEGIN IMMEDIATE"))
            repository = StorageRepository(session)
            if any(
                _competing_exit(pending, intent)
                for pending in repository.list_unresolved_order_intents()
            ):
                raise OrderSubmissionBlocked(
                    decision.model_copy(
                        update={"allowed": False, "reasons": ("EXIT_REDUCING_ORDER_PENDING",)}
                    )
                )
            existing = repository.get_order_intent(intent.intent_id)
            if existing is None:
                repository.create_order_intent(intent)
            elif existing != intent:
                raise StateConflictError("exit claim conflicts with its durable intent")
            return repository.transition_order_intent(
                intent.intent_id,
                IntentState.ORDER_CREATED,
                expected_current=IntentState.RISK_APPROVED,
            )

    def _transition(
        self,
        intent_id: str,
        target: IntentState,
        expected: IntentState,
    ) -> OrderIntent:
        with self._database.transaction() as session:
            return StorageRepository(session).transition_order_intent(
                intent_id,
                target,
                expected_current=expected,
            )

    def _adopt(self, intent: OrderIntent, order: BrokerOrder) -> OrderIntent:
        try:
            with self._database.transaction() as session:
                return persist_broker_acknowledgement(StorageRepository(session), intent, order)
        except (InvalidOrderTransition, StateConflictError) as error:
            raise BrokerProtocolError(
                "broker acknowledgement conflicts with durable order state"
            ) from error

    def _record_unknown_incident(self, intent: OrderIntent, retries: int) -> None:
        digest = hashlib.sha256(intent.intent_id.encode("utf-8")).hexdigest()
        with self._database.transaction() as session:
            StorageRepository(session).create_incident(
                f"submission-unknown-{digest}",
                severity="ERROR",
                kind="SUBMISSION_UNKNOWN",
                message="Broker submission outcome remains uncertain after bounded recovery",
                occurred_at=intent.created_at,
                detail={
                    "client_order_id": intent.client_order_id,
                    "not_found_retries": retries,
                },
            )

    def _stored_intent(self, expected: OrderIntent) -> OrderIntent:
        with self._database.transaction() as session:
            stored = StorageRepository(session).get_order_intent(expected.intent_id)
        if stored is None:
            raise ValueError(f"durable order intent not found: {expected.intent_id}")
        if stored.model_copy(update={"state": expected.state}) != expected:
            raise BrokerProtocolError(
                f"durable order intent {expected.intent_id} does not match the requested order"
            )
        return stored

    def _stored_order(self, intent: OrderIntent) -> BrokerOrder:
        with self._database.transaction() as session:
            order = StorageRepository(session).get_broker_order_by_client_order_id(
                intent.client_order_id
            )
        if order is None:
            raise BrokerProtocolError(
                f"accepted order intent {intent.intent_id} has no durable broker order"
            )
        _assert_ack_matches_intent(intent, order)
        return order

    async def _recover_persisted_submission(self, intent: OrderIntent) -> SubmissionResult:
        try:
            found = await self._broker.get_order_by_client_id(
                intent.client_order_id,
                expected=intent,
            )
        except (BrokerRequestError, BrokerTransportError) as error:
            self._record_unknown_incident(intent, 0)
            raise SubmissionUncertainError(
                "submission remains uncertain because lookup failed"
            ) from error
        if found is None:
            unknown = intent
            if intent.state is IntentState.SUBMITTING:
                unknown = self._transition(
                    intent.intent_id,
                    IntentState.SUBMISSION_UNKNOWN,
                    IntentState.SUBMITTING,
                )
            self._record_unknown_incident(unknown, 0)
            raise SubmissionUncertainError("submission remains uncertain after restart lookup")
        try:
            adopted = self._adopt(intent, found)
        except BrokerProtocolError:
            self._transition(intent.intent_id, IntentState.ERROR, intent.state)
            raise
        return SubmissionResult(
            intent=adopted,
            broker_order=found,
            recovered_after_ambiguity=True,
            not_found_retries=0,
        )

    async def submit_durable(
        self,
        intent: OrderIntent,
        *,
        purpose: OrderPurpose,
        market_eligible: bool,
        drawdown: DrawdownState | None,
        risk_approved: bool | None,
        vetoes: PortfolioVetoDecision | None = None,
    ) -> SubmissionResult:
        """Submit or recover one pre-persisted intent without ever replacing its identity."""
        stored = self._stored_intent(intent)
        if stored.state in {IntentState.SUBMITTING, IntentState.SUBMISSION_UNKNOWN}:
            return await self._recover_persisted_submission(stored)
        if stored.state in {
            IntentState.BROKER_ACCEPTED,
            IntentState.PARTIALLY_FILLED,
            IntentState.FILLED,
        }:
            return SubmissionResult(intent=stored, broker_order=self._stored_order(stored))
        if stored.state not in {IntentState.RISK_APPROVED, IntentState.ORDER_CREATED}:
            raise OrderIntentNotResumable(stored)

        if stored.state is IntentState.RISK_APPROVED:
            initial_account = await self._broker.get_account()
            initial_positions = tuple(await self._broker.get_positions())
            initial_gate = self._gate(
                initial_account,
                initial_positions,
                stored,
                purpose,
                market_eligible,
                drawdown,
                risk_approved,
                vetoes,
                await self._open_exit_orders(purpose),
            )
            if not initial_gate.allowed:
                raise OrderSubmissionBlocked(initial_gate)
            if purpose in {OrderPurpose.STRATEGY_EXIT, OrderPurpose.DRAWDOWN_LIQUIDATION}:
                stored = self._claim_exit(stored, initial_gate)
            else:
                stored = self._transition(
                    stored.intent_id,
                    IntentState.ORDER_CREATED,
                    IntentState.RISK_APPROVED,
                )
        return await self._submit_order_created(
            stored,
            purpose=purpose,
            market_eligible=market_eligible,
            drawdown=drawdown,
            risk_approved=risk_approved,
            vetoes=vetoes,
        )

    async def reconcile_durable(self, intent: OrderIntent) -> SubmissionResult:
        """Look up a possible or accepted send without creating a broker mutation."""
        stored = self._stored_intent(intent)
        if stored.state in {IntentState.SUBMITTING, IntentState.SUBMISSION_UNKNOWN}:
            return await self._recover_persisted_submission(stored)
        if stored.state not in {
            IntentState.BROKER_ACCEPTED,
            IntentState.PARTIALLY_FILLED,
        }:
            raise OrderIntentNotResumable(stored)
        try:
            found = await self._broker.get_order_by_client_id(
                stored.client_order_id,
                expected=stored,
            )
        except (BrokerRequestError, BrokerTransportError) as error:
            raise SubmissionUncertainError(
                "accepted order state cannot be reconciled because broker lookup failed"
            ) from error
        if found is None:
            raise SubmissionUncertainError(
                "accepted order state cannot be reconciled because the broker did not find it"
            )
        try:
            _assert_ack_matches_intent(stored, found)
            target = _ack_terminal_target(found)
            with self._database.transaction() as session:
                repository = StorageRepository(session)
                durable_order = repository.get_broker_order_by_client_order_id(
                    stored.client_order_id
                )
                if durable_order is None:
                    raise BrokerProtocolError(
                        f"accepted order intent {stored.intent_id} has no durable broker order"
                    )
                if durable_order.broker_order_id != found.broker_order_id:
                    raise BrokerProtocolError(
                        "broker lookup changed the durable broker order identity"
                    )
                repository.update_broker_order_lifecycle(found)
                reconciled = stored
                if target is not None and target is not stored.state:
                    reconciled = repository.transition_order_intent(
                        stored.intent_id,
                        target,
                        expected_current=stored.state,
                    )
        except BrokerProtocolError:
            self._transition(stored.intent_id, IntentState.ERROR, stored.state)
            raise
        except (InvalidOrderTransition, StateConflictError) as error:
            self._transition(stored.intent_id, IntentState.ERROR, stored.state)
            raise BrokerProtocolError(
                "broker lifecycle conflicts with durable order state"
            ) from error
        return SubmissionResult(intent=reconciled, broker_order=found)

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
        """Submit once, then recover or retry only under the bounded same-ID policy."""
        if intent.state is not IntentState.RISK_APPROVED:
            raise ValueError("new order intent must begin in RISK_APPROVED")

        initial_account = await self._broker.get_account()
        initial_positions = tuple(await self._broker.get_positions())
        initial_gate = self._gate(
            initial_account,
            initial_positions,
            intent,
            purpose,
            market_eligible,
            drawdown,
            risk_approved,
            vetoes,
            await self._open_exit_orders(purpose),
        )
        if not initial_gate.allowed:
            raise OrderSubmissionBlocked(initial_gate)

        if purpose in {OrderPurpose.STRATEGY_EXIT, OrderPurpose.DRAWDOWN_LIQUIDATION}:
            self._claim_exit(intent, initial_gate)
        else:
            with self._database.transaction() as session:
                repository = StorageRepository(session)
                repository.create_order_intent(intent)
                repository.transition_order_intent(
                    intent.intent_id,
                    IntentState.ORDER_CREATED,
                    expected_current=IntentState.RISK_APPROVED,
                )

        return await self._submit_order_created(
            intent.model_copy(update={"state": IntentState.ORDER_CREATED}),
            purpose=purpose,
            market_eligible=market_eligible,
            drawdown=drawdown,
            risk_approved=risk_approved,
            vetoes=vetoes,
        )

    async def _acquire_send_constraints(self, intent: OrderIntent, purpose: OrderPurpose) -> None:
        """Optional provider constraints acquired before the final account/position reads."""

    async def _acquire_final_market_data(self, intent: OrderIntent, purpose: OrderPurpose) -> None:
        """Optional volatile market evidence acquired after inventory, before all final gates."""

    async def _submit_order_created(
        self,
        intent: OrderIntent,
        *,
        purpose: OrderPurpose,
        market_eligible: bool,
        drawdown: DrawdownState | None,
        risk_approved: bool | None,
        vetoes: PortfolioVetoDecision | None,
    ) -> SubmissionResult:
        """Recheck volatile gates and submit an intent already durable as ORDER_CREATED."""
        if intent.state is not IntentState.ORDER_CREATED:
            raise ValueError("submission requires an ORDER_CREATED intent")

        await self._acquire_send_constraints(intent, purpose)
        fresh_account = await self._broker.get_account()
        fresh_positions = tuple(await self._broker.get_positions())
        fresh_open_orders = await self._open_exit_orders(purpose)
        await self._acquire_final_market_data(intent, purpose)
        fresh_gate = self._gate(
            fresh_account,
            fresh_positions,
            intent,
            purpose,
            market_eligible,
            drawdown,
            risk_approved,
            vetoes,
            fresh_open_orders,
        )
        # Lifetime/ownership authority is consumed after the last awaited acquisition.
        # Recovery of a possibly sent intent bypasses this prepared-send path entirely.
        if fresh_gate.allowed and self._final_send_check is not None:
            objections = self._final_send_check(intent, purpose)
            if objections:
                fresh_gate = fresh_gate.model_copy(
                    update={"allowed": False, "reasons": (*fresh_gate.reasons, *objections)}
                )
        if self._transactional_send_check is not None:
            # This hook and send-state CAS share the same durable writer fence. No
            # provider await occurs under the lock, and only this coordinator POSTs.
            with self._database.transaction() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                objections = (
                    self._transactional_send_check(session, intent, purpose)
                    if fresh_gate.allowed
                    else ()
                )
                if objections:
                    fresh_gate = fresh_gate.model_copy(
                        update={"allowed": False, "reasons": (*fresh_gate.reasons, *objections)}
                    )
                if not fresh_gate.allowed and self._transactional_send_refusal is not None:
                    self._transactional_send_refusal(session, intent, purpose, fresh_gate.reasons)
                submitting = StorageRepository(session).transition_order_intent(
                    intent.intent_id,
                    IntentState.SUBMITTING if fresh_gate.allowed else IntentState.ERROR,
                    expected_current=IntentState.ORDER_CREATED,
                )
            if not fresh_gate.allowed:
                raise OrderSubmissionBlocked(fresh_gate)
        elif not fresh_gate.allowed:
            self._transition(intent.intent_id, IntentState.ERROR, IntentState.ORDER_CREATED)
            raise OrderSubmissionBlocked(fresh_gate)
        else:
            submitting = self._transition(
                intent.intent_id, IntentState.SUBMITTING, IntentState.ORDER_CREATED
            )
        broker_intent = intent.model_copy(update={"state": IntentState.RISK_APPROVED})
        retries = 0
        recovered = False
        while True:
            try:
                order = await self._broker.submit_order(broker_intent)
            except BrokerRequestError as error:
                if retries == 0 and error.status_code is not None and error.status_code < 500:
                    self._transition(intent.intent_id, IntentState.REJECTED, IntentState.SUBMITTING)
                    raise
                submitting = self._transition(
                    intent.intent_id,
                    IntentState.SUBMISSION_UNKNOWN,
                    IntentState.SUBMITTING,
                )
            except (BrokerTransportError, SubmissionUncertainError):
                submitting = self._transition(
                    intent.intent_id,
                    IntentState.SUBMISSION_UNKNOWN,
                    IntentState.SUBMITTING,
                )
            except BrokerProtocolError:
                self._transition(intent.intent_id, IntentState.ERROR, IntentState.SUBMITTING)
                raise
            else:
                try:
                    adopted = self._adopt(submitting, order)
                except BrokerProtocolError:
                    self._transition(intent.intent_id, IntentState.ERROR, submitting.state)
                    raise
                return SubmissionResult(
                    intent=adopted,
                    broker_order=order,
                    recovered_after_ambiguity=recovered,
                    not_found_retries=retries,
                )

            try:
                found = await self._broker.get_order_by_client_id(
                    intent.client_order_id,
                    expected=broker_intent,
                )
            except (BrokerRequestError, BrokerTransportError) as lookup_error:
                self._record_unknown_incident(intent, retries)
                raise SubmissionUncertainError(
                    "submission remains uncertain because lookup failed"
                ) from lookup_error
            if found is not None:
                adopted = self._adopt(submitting, found)
                return SubmissionResult(
                    intent=adopted,
                    broker_order=found,
                    recovered_after_ambiguity=True,
                    not_found_retries=retries,
                )
            if retries >= self._submission_policy.max_not_found_retries:
                self._record_unknown_incident(intent, retries)
                raise SubmissionUncertainError(
                    "submission remains uncertain after bounded same-ID recovery"
                )
            submitting = self._transition(
                intent.intent_id,
                IntentState.SUBMITTING,
                IntentState.SUBMISSION_UNKNOWN,
            )
            retries += 1
            recovered = True
