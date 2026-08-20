"""Idempotent application of normalized broker order events."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType

from pydantic import Field, model_validator

from quantbot.brokers import TradeStreamIncident, TradeUpdate, TradeUpdateEvent
from quantbot.brokers.base import BrokerModel
from quantbot.domain import (
    BrokerOrder,
    IntentState,
    InvalidOrderTransition,
    OrderIntent,
    transition_order,
)
from quantbot.storage import StateConflictError, StorageRepository


class OrderEventReconciliationRequired(RuntimeError):
    """Raised when event identity or lifecycle cannot be resolved locally."""


class AppliedOrderEvent(BrokerModel):
    """Stable event identity retained across reconnects and duplicate delivery."""

    event_id: str = Field(min_length=1)
    event: TradeUpdateEvent
    occurred_at: datetime
    fingerprint: str = Field(min_length=64, max_length=64)


class OrderEventState(BrokerModel):
    """Current intent state plus the ordered event identities already observed."""

    intent_state: IntentState
    applied_events: tuple[AppliedOrderEvent, ...] = ()

    @model_validator(mode="after")
    def validate_event_identity(self) -> OrderEventState:
        identifiers = [event.event_id for event in self.applied_events]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("applied order event IDs must be unique")
        return self


_EVENT_STATE: Mapping[TradeUpdateEvent, IntentState | None] = MappingProxyType(
    {
        TradeUpdateEvent.ACCEPTED: IntentState.BROKER_ACCEPTED,
        TradeUpdateEvent.PENDING_NEW: IntentState.BROKER_ACCEPTED,
        TradeUpdateEvent.NEW: IntentState.BROKER_ACCEPTED,
        TradeUpdateEvent.HELD: IntentState.BROKER_ACCEPTED,
        TradeUpdateEvent.PARTIAL_FILL: IntentState.PARTIALLY_FILLED,
        TradeUpdateEvent.FILL: IntentState.FILLED,
        TradeUpdateEvent.PENDING_CANCEL: IntentState.CANCEL_PENDING,
        TradeUpdateEvent.CANCELED: IntentState.CANCELLED,
        TradeUpdateEvent.EXPIRED: IntentState.EXPIRED,
        TradeUpdateEvent.REJECTED: IntentState.REJECTED,
        TradeUpdateEvent.DONE_FOR_DAY: None,
        TradeUpdateEvent.REPLACED: None,
        TradeUpdateEvent.STOPPED: None,
        TradeUpdateEvent.PENDING_REPLACE: None,
        TradeUpdateEvent.CALCULATED: None,
        TradeUpdateEvent.SUSPENDED: None,
        TradeUpdateEvent.ORDER_REPLACE_REJECTED: None,
        TradeUpdateEvent.ORDER_CANCEL_REJECTED: None,
        TradeUpdateEvent.TRADE_BUST: None,
        TradeUpdateEvent.TRADE_CORRECT: None,
    }
)

_IGNORABLE_OUT_OF_ORDER_TRANSITIONS = frozenset(
    {
        (IntentState.PARTIALLY_FILLED, IntentState.BROKER_ACCEPTED),
        (IntentState.CANCEL_PENDING, IntentState.BROKER_ACCEPTED),
        (IntentState.FILLED, IntentState.BROKER_ACCEPTED),
        (IntentState.FILLED, IntentState.PARTIALLY_FILLED),
        (IntentState.FILLED, IntentState.CANCEL_PENDING),
        (IntentState.FILLED, IntentState.CANCELLED),
    }
)


def intent_state_for_trade_update(event: TradeUpdateEvent) -> IntentState | None:
    """Return the explicit normalized state target for every documented event."""
    return _EVENT_STATE[event]


def _event_fingerprint(update: TradeUpdate) -> str:
    payload = update.model_dump(mode="json", exclude={"connection_generation"})
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _next_state(current: IntentState, target: IntentState) -> IntentState:
    try:
        return transition_order(current, target)
    except InvalidOrderTransition as error:
        if (current, target) in _IGNORABLE_OUT_OF_ORDER_TRANSITIONS:
            return current
        raise OrderEventReconciliationRequired(
            f"broker event requires an invalid intent transition: {current.value} -> {target.value}"
        ) from error


def apply_order_update(state: OrderEventState, update: TradeUpdate) -> OrderEventState:
    """Apply one event exactly once without allowing out-of-order state regression."""
    fingerprint = _event_fingerprint(update)
    existing = next(
        (event for event in state.applied_events if event.event_id == update.event_id),
        None,
    )
    if existing is not None:
        if existing.fingerprint == fingerprint:
            return state
        raise OrderEventReconciliationRequired(
            f"conflicting duplicate order event: {update.event_id}"
        )

    target = intent_state_for_trade_update(update.event)
    next_state = state.intent_state if target is None else _next_state(state.intent_state, target)
    applied = AppliedOrderEvent(
        event_id=update.event_id,
        event=update.event,
        occurred_at=update.occurred_at,
        fingerprint=fingerprint,
    )
    return state.model_copy(
        update={
            "intent_state": next_state,
            "applied_events": (*state.applied_events, applied),
        }
    )


def _stable_order_fields(order: BrokerOrder) -> tuple[object, ...]:
    """Fields that economically identify an order and must never change.

    submitted_at is excluded: Alpaca re-stamps it when an order accepted while the market is
    closed is released into the next session, so including it made every queued order look
    like a conflicting identity the moment it actually reached the market.
    """
    return (
        order.broker_order_id,
        order.client_order_id,
        order.symbol,
        order.side,
        order.order_type,
        order.time_in_force,
        order.quantity,
    )


def _durable_event_detail(update: TradeUpdate) -> dict[str, object]:
    return update.model_dump(mode="python", exclude={"connection_generation"})


def persist_order_update(
    repository: StorageRepository,
    intent_id: str,
    update: TradeUpdate,
) -> OrderIntent:
    """Persist and apply one update inside the caller-owned database transaction."""
    intent = repository.get_order_intent(intent_id)
    if intent is None:
        raise OrderEventReconciliationRequired(f"order intent not found for event: {intent_id}")
    if intent.client_order_id != update.order.client_order_id:
        raise OrderEventReconciliationRequired(
            "broker event client_order_id does not match the local intent"
        )

    stored_order = repository.get_broker_order(update.order.broker_order_id)
    try:
        if stored_order is None:
            repository.save_broker_order(update.order)
        elif _stable_order_fields(stored_order) != _stable_order_fields(update.order):
            raise OrderEventReconciliationRequired(
                "broker event conflicts with the stored order identity"
            )
        created = repository.record_order_event(
            update.event_id,
            event_type=update.event.value,
            occurred_at=update.occurred_at,
            intent_id=intent_id,
            broker_order_id=update.order.broker_order_id,
            detail=_durable_event_detail(update),
        )
    except StateConflictError as error:
        raise OrderEventReconciliationRequired(
            f"conflicting duplicate durable order event: {update.event_id}"
        ) from error

    if not created:
        if update.fill is not None and repository.get_fill(update.fill.fill_id) != update.fill:
            raise OrderEventReconciliationRequired(
                "duplicate order event is missing its exact durable fill"
            )
        return intent

    if update.fill is not None:
        try:
            repository.record_fill(update.fill)
        except StateConflictError as error:
            raise OrderEventReconciliationRequired(
                f"conflicting duplicate execution: {update.fill.fill_id}"
            ) from error

    target = intent_state_for_trade_update(update.event)
    if target is None:
        return intent
    next_state = _next_state(intent.state, target)
    if next_state is intent.state:
        return intent
    return repository.transition_order_intent(
        intent_id,
        next_state,
        expected_current=intent.state,
    )


def persist_trade_stream_incident(
    repository: StorageRepository,
    incident: TradeStreamIncident,
    *,
    run_id: str | None = None,
) -> bool:
    """Persist the stream's stable, secret-safe incident representation."""
    try:
        return repository.create_incident(
            incident.incident_id,
            severity=incident.severity,
            kind=incident.kind,
            message=incident.message,
            occurred_at=incident.occurred_at,
            run_id=run_id,
            detail=incident.detail,
        )
    except StateConflictError as error:
        raise OrderEventReconciliationRequired(
            f"conflicting duplicate trade stream incident: {incident.incident_id}"
        ) from error
