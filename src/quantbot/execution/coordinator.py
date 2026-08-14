"""Durable order submission with bounded same-identity ambiguity recovery."""

from __future__ import annotations

import hashlib

from pydantic import Field

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
from quantbot.domain import BrokerOrder, IntentState, OrderIntent, ReconciliationStatus
from quantbot.risk import DrawdownState, OrderGateDecision, OrderPurpose, evaluate_order_gate
from quantbot.storage import Database, StorageRepository


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
    if order.filled_quantity > 0 or status in {"partially_filled", "partial_fill"}:
        return IntentState.PARTIALLY_FILLED
    if status in {"canceled", "cancelled"}:
        return IntentState.CANCELLED
    if status == "expired":
        return IntentState.EXPIRED
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


class ExecutionCoordinator:
    """Persist before network I/O and never replace a client order identity."""

    def __init__(
        self,
        database: Database,
        broker: BrokerAdapter,
        settings: Settings,
        *,
        submission_policy: SubmissionPolicy | None = None,
    ) -> None:
        self._database = database
        self._broker = broker
        self._settings = settings
        self._submission_policy = submission_policy or SubmissionPolicy()

    def _gate(
        self,
        account_snapshot: BrokerAccountSnapshot,
        purpose: OrderPurpose,
        market_eligible: bool,
        drawdown: DrawdownState | None,
        risk_approved: bool | None,
    ) -> OrderGateDecision:
        with self._database.transaction() as session:
            repository = StorageRepository(session)
            durable_kill = repository.get_kill_switch_state().engaged
            latest_reconciliation = repository.get_latest_reconciliation()
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
        )
        reasons = list(decision.reasons)
        if not account_snapshot.order_entry_allowed:
            reasons.append("BROKER_ORDER_ENTRY_DISABLED")
        if account_snapshot.restrictions:
            reasons.append("BROKER_ACCOUNT_RESTRICTED")
        return decision.model_copy(
            update={"allowed": not reasons, "reasons": tuple(dict.fromkeys(reasons))}
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
        with self._database.transaction() as session:
            return persist_broker_acknowledgement(StorageRepository(session), intent, order)

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

    async def submit(
        self,
        intent: OrderIntent,
        *,
        purpose: OrderPurpose,
        market_eligible: bool,
        drawdown: DrawdownState | None,
        risk_approved: bool | None,
    ) -> SubmissionResult:
        """Submit once, then recover or retry only under the bounded same-ID policy."""
        if intent.state is not IntentState.RISK_APPROVED:
            raise ValueError("new order intent must begin in RISK_APPROVED")

        initial_account = await self._broker.get_account()
        initial_gate = self._gate(
            initial_account,
            purpose,
            market_eligible,
            drawdown,
            risk_approved,
        )
        if not initial_gate.allowed:
            raise OrderSubmissionBlocked(initial_gate)

        with self._database.transaction() as session:
            repository = StorageRepository(session)
            repository.create_order_intent(intent)
            repository.transition_order_intent(
                intent.intent_id,
                IntentState.ORDER_CREATED,
                expected_current=IntentState.RISK_APPROVED,
            )

        fresh_account = await self._broker.get_account()
        await self._broker.get_positions()
        fresh_gate = self._gate(
            fresh_account,
            purpose,
            market_eligible,
            drawdown,
            risk_approved,
        )
        if not fresh_gate.allowed:
            self._transition(intent.intent_id, IntentState.ERROR, IntentState.ORDER_CREATED)
            raise OrderSubmissionBlocked(fresh_gate)

        submitting = self._transition(
            intent.intent_id,
            IntentState.SUBMITTING,
            IntentState.ORDER_CREATED,
        )
        retries = 0
        recovered = False
        while True:
            try:
                order = await self._broker.submit_order(intent)
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
                    expected=intent,
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
