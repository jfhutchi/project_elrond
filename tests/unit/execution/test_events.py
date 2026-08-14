from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.brokers import TradeStreamIncident, TradeUpdate, TradeUpdateEvent
from quantbot.domain import (
    BrokerOrder,
    Fill,
    IntentState,
    OrderIntent,
    OrderSide,
    OrderType,
    TimeInForce,
)
from quantbot.execution import (
    OrderEventReconciliationRequired,
    OrderEventState,
    apply_order_update,
    intent_state_for_trade_update,
    persist_order_update,
    persist_trade_stream_incident,
)
from quantbot.storage import Database, StorageRepository

NOW = datetime(2026, 8, 31, 20, tzinfo=UTC)


def broker_order(*, side: OrderSide = OrderSide.BUY) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id="broker-order-1",
        client_order_id="qb-client-1",
        symbol="SPY",
        side=side,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal("10"),
        filled_quantity=Decimal("4"),
        status="partially_filled",
        submitted_at=NOW,
        filled_average_price=Decimal("100"),
    )


def update(
    event: TradeUpdateEvent,
    *,
    event_id: str | None = None,
    occurred_at: datetime = NOW,
    price: str = "100",
) -> TradeUpdate:
    has_fill = event in {TradeUpdateEvent.FILL, TradeUpdateEvent.PARTIAL_FILL}
    resolved_id = event_id or f"event-{event.value}"
    fill = (
        Fill(
            fill_id=resolved_id,
            broker_order_id="broker-order-1",
            symbol="SPY",
            side=OrderSide.BUY,
            quantity=Decimal("4"),
            price=Decimal(price),
            occurred_at=occurred_at,
        )
        if has_fill
        else None
    )
    return TradeUpdate(
        event=event,
        event_id=resolved_id,
        order=broker_order(),
        occurred_at=occurred_at,
        fill=fill,
        position_quantity=Decimal("4") if has_fill else None,
        connection_generation=0,
    )


def order_intent(*, state: IntentState = IntentState.BROKER_ACCEPTED) -> OrderIntent:
    return OrderIntent(
        intent_id="intent-1",
        client_order_id="qb-client-1",
        strategy_id="full-v1:test",
        signal_date=date(2026, 8, 31),
        symbol="SPY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal("10"),
        state=state,
        created_at=NOW,
    )


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (TradeUpdateEvent.ACCEPTED, IntentState.BROKER_ACCEPTED),
        (TradeUpdateEvent.PENDING_NEW, IntentState.BROKER_ACCEPTED),
        (TradeUpdateEvent.NEW, IntentState.BROKER_ACCEPTED),
        (TradeUpdateEvent.HELD, IntentState.BROKER_ACCEPTED),
        (TradeUpdateEvent.PARTIAL_FILL, IntentState.PARTIALLY_FILLED),
        (TradeUpdateEvent.FILL, IntentState.FILLED),
        (TradeUpdateEvent.PENDING_CANCEL, IntentState.CANCEL_PENDING),
        (TradeUpdateEvent.CANCELED, IntentState.CANCELLED),
        (TradeUpdateEvent.EXPIRED, IntentState.EXPIRED),
        (TradeUpdateEvent.REJECTED, IntentState.REJECTED),
        (TradeUpdateEvent.DONE_FOR_DAY, None),
        (TradeUpdateEvent.REPLACED, None),
        (TradeUpdateEvent.STOPPED, None),
        (TradeUpdateEvent.PENDING_REPLACE, None),
        (TradeUpdateEvent.CALCULATED, None),
        (TradeUpdateEvent.SUSPENDED, None),
        (TradeUpdateEvent.ORDER_REPLACE_REJECTED, None),
        (TradeUpdateEvent.ORDER_CANCEL_REJECTED, None),
        (TradeUpdateEvent.TRADE_BUST, None),
        (TradeUpdateEvent.TRADE_CORRECT, None),
    ],
)
def test_all_documented_updates_have_an_explicit_intent_mapping(
    event: TradeUpdateEvent,
    expected: IntentState | None,
) -> None:
    assert intent_state_for_trade_update(event) is expected


def test_exact_duplicate_event_is_idempotent_and_conflict_requires_reconciliation() -> None:
    state = OrderEventState(intent_state=IntentState.BROKER_ACCEPTED, applied_events=())
    event = update(TradeUpdateEvent.PARTIAL_FILL, event_id="execution-1")
    once = apply_order_update(state, event)

    assert apply_order_update(once, event) is once
    with pytest.raises(OrderEventReconciliationRequired, match="conflicting duplicate"):
        apply_order_update(
            once,
            update(TradeUpdateEvent.PARTIAL_FILL, event_id="execution-1", price="101"),
        )


def test_out_of_order_acknowledgement_is_recorded_without_state_regression() -> None:
    partial = apply_order_update(
        OrderEventState(intent_state=IntentState.BROKER_ACCEPTED, applied_events=()),
        update(TradeUpdateEvent.PARTIAL_FILL, event_id="execution-1"),
    )

    result = apply_order_update(
        partial,
        update(
            TradeUpdateEvent.NEW,
            event_id="event-new",
            occurred_at=NOW - timedelta(seconds=1),
        ),
    )

    assert result.intent_state is IntentState.PARTIALLY_FILLED
    assert tuple(event.event_id for event in result.applied_events) == (
        "execution-1",
        "event-new",
    )


def test_cancel_fill_race_keeps_fill_terminal_when_cancel_arrives_late() -> None:
    state = OrderEventState(intent_state=IntentState.CANCEL_PENDING, applied_events=())
    filled = apply_order_update(
        state,
        update(TradeUpdateEvent.FILL, event_id="execution-final"),
    )
    result = apply_order_update(
        filled,
        update(
            TradeUpdateEvent.CANCELED,
            event_id="event-canceled",
            occurred_at=NOW + timedelta(seconds=1),
        ),
    )

    assert result.intent_state is IntentState.FILLED
    assert len(result.applied_events) == 2


def test_direct_broker_cancellation_is_valid_without_a_local_pending_event() -> None:
    result = apply_order_update(
        OrderEventState(intent_state=IntentState.BROKER_ACCEPTED, applied_events=()),
        update(TradeUpdateEvent.CANCELED),
    )

    assert result.intent_state is IntentState.CANCELLED


def test_fill_after_rejection_requires_authoritative_reconciliation() -> None:
    rejected = apply_order_update(
        OrderEventState(intent_state=IntentState.BROKER_ACCEPTED, applied_events=()),
        update(TradeUpdateEvent.REJECTED),
    )

    with pytest.raises(OrderEventReconciliationRequired, match="transition"):
        apply_order_update(rejected, update(TradeUpdateEvent.FILL, event_id="execution-1"))


def test_conflicting_terminal_events_require_authoritative_reconciliation() -> None:
    rejected = apply_order_update(
        OrderEventState(intent_state=IntentState.BROKER_ACCEPTED, applied_events=()),
        update(TradeUpdateEvent.REJECTED),
    )

    with pytest.raises(OrderEventReconciliationRequired, match="transition"):
        apply_order_update(
            rejected,
            update(TradeUpdateEvent.CANCELED, event_id="event-canceled"),
        )


def test_non_state_event_is_still_recorded_for_audit() -> None:
    state = OrderEventState(intent_state=IntentState.BROKER_ACCEPTED, applied_events=())

    result = apply_order_update(state, update(TradeUpdateEvent.DONE_FOR_DAY))

    assert result.intent_state is IntentState.BROKER_ACCEPTED
    assert result.applied_events[0].event is TradeUpdateEvent.DONE_FOR_DAY


def test_order_update_is_durable_and_duplicate_safe_across_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "events.sqlite"
    event = update(TradeUpdateEvent.PARTIAL_FILL, event_id="execution-1")
    with Database(database_path) as database:
        with database.transaction() as session:
            repository = StorageRepository(session)
            repository.create_order_intent(order_intent())
            repository.save_broker_order(broker_order())
            persisted = persist_order_update(repository, "intent-1", event)
            assert persisted.state is IntentState.PARTIALLY_FILLED

    with Database(database_path) as reopened:
        with reopened.transaction() as session:
            repository = StorageRepository(session)
            duplicate = persist_order_update(
                repository,
                "intent-1",
                event.model_copy(update={"connection_generation": 1}),
            )
            assert duplicate.state is IntentState.PARTIALLY_FILLED
            assert len(repository.list_order_events(intent_id="intent-1")) == 1
            assert repository.list_fills(broker_order_id="broker-order-1") == [event.fill]


def test_conflicting_durable_event_rolls_back_without_mutating_original(tmp_path: Path) -> None:
    database_path = tmp_path / "events.sqlite"
    original = update(TradeUpdateEvent.PARTIAL_FILL, event_id="execution-1")
    with Database(database_path) as database:
        with database.transaction() as session:
            repository = StorageRepository(session)
            repository.create_order_intent(order_intent())
            repository.save_broker_order(broker_order())
            persist_order_update(repository, "intent-1", original)

        with pytest.raises(OrderEventReconciliationRequired, match="conflicting duplicate"):
            with database.transaction() as session:
                persist_order_update(
                    StorageRepository(session),
                    "intent-1",
                    update(
                        TradeUpdateEvent.PARTIAL_FILL,
                        event_id="execution-1",
                        price="101",
                    ),
                )

        with database.transaction() as session:
            repository = StorageRepository(session)
            assert repository.get_order_intent("intent-1").state is IntentState.PARTIALLY_FILLED  # type: ignore[union-attr]
            assert repository.get_fill("execution-1") == original.fill


def test_unknown_stream_incident_has_a_durable_idempotent_mapping(tmp_path: Path) -> None:
    incident = TradeStreamIncident(
        incident_id="alpaca-stream-incident-1",
        severity="ERROR",
        kind="UNKNOWN_BROKER_ORDER_EVENT",
        message="Alpaca reported an unknown trade update event",
        occurred_at=NOW,
        detail={"broker_order_id": "broker-order-1", "event": "future_status"},
    )
    database_path = tmp_path / "incidents.sqlite"
    with Database(database_path) as database:
        with database.transaction() as session:
            repository = StorageRepository(session)
            assert persist_trade_stream_incident(repository, incident) is True
            assert persist_trade_stream_incident(repository, incident) is False

    with Database(database_path) as reopened:
        with reopened.transaction() as session:
            stored = StorageRepository(session).list_incidents(unresolved_only=True)
            assert len(stored) == 1
            assert stored[0].incident_id == incident.incident_id
            assert stored[0].kind == incident.kind
            assert stored[0].detail == dict(incident.detail)
