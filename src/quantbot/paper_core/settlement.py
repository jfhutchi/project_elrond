"""Shared read-only settlement authority for the core and retained ownership monitor."""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from quantbot.brokers.base import BrokerProtocolError
from quantbot.domain import IntentState, OrderIntent
from quantbot.execution.coordinator import _assert_ack_matches_intent
from quantbot.paper_core.contracts import BrokerEvent, CoreConfig
from quantbot.paper_core.store import table
from quantbot.research.manifest import content_id
from quantbot.storage import StorageRepository

REFUSAL = "CORE_ENTRY_SEND_REFUSED"
EXIT_REFUSAL = "CORE_OWNED_EXIT_SEND_REFUSED"


def definitively_unsent(repository: StorageRepository, intent_id: str) -> bool:
    intent = repository.get_order_intent(intent_id)
    if intent is None or intent.state not in {IntentState.ERROR, IntentState.REJECTED}:
        return False
    if repository.get_broker_order_by_client_order_id(intent.client_order_id) is not None:
        return False
    if intent.state is IntentState.REJECTED:
        return any(
            e.event_type == "CORE_LOCAL_SEND_NOT_ATTEMPTED"
            and e.detail.get("contract") == "ACCOUNT_BUDGET_BEFORE_IO_V1"
            and e.detail.get("client_order_id") == intent.client_order_id
            and e.detail.get("definitively_unsent") is True
            for e in repository.list_order_events(intent_id=intent_id)
        )
    # ORDER_CREATED cannot follow SUBMITTING under the retained lifecycle. The guard
    # latches refusal before leaving that state; later guard invocations never POST it.
    return any(
        event.event_type in {REFUSAL, EXIT_REFUSAL}
        and event.detail.get("definitively_unsent") is True
        and event.detail.get("state_at_refusal") == IntentState.ORDER_CREATED.value
        and event.detail.get("guard_contract") == "core-final-send-v1"
        for event in repository.list_order_events(intent_id=intent_id)
    )


def definitively_unsent_many(
    repository: StorageRepository, intent_ids: Sequence[str]
) -> dict[str, bool]:
    """Same definitive refusal predicate, using fresh data from one caller-owned snapshot."""
    unique = list(dict.fromkeys(intent_ids))
    result = {identity: False for identity in unique}
    eligible = [
        (identity, intent)
        for identity, intent in zip(unique, repository.get_order_intents(unique), strict=True)
        if intent is not None and intent.state in {IntentState.ERROR, IntentState.REJECTED}
    ]
    orders = repository.get_broker_orders_by_client_order_ids(
        [intent.client_order_id for _, intent in eligible]
    )
    candidates = [
        (identity, intent)
        for (identity, intent), order in zip(eligible, orders, strict=True)
        if order is None
    ]
    histories = repository.get_order_events_for_intents([identity for identity, _ in candidates])
    for identity, intent in candidates:
        events = histories[identity]
        if intent.state is IntentState.REJECTED:
            result[identity] = any(
                event.event_type == "CORE_LOCAL_SEND_NOT_ATTEMPTED"
                and event.detail.get("contract") == "ACCOUNT_BUDGET_BEFORE_IO_V1"
                and event.detail.get("client_order_id") == intent.client_order_id
                and event.detail.get("definitively_unsent") is True
                for event in events
            )
        else:
            result[identity] = any(
                event.event_type in {REFUSAL, EXIT_REFUSAL}
                and event.detail.get("definitively_unsent") is True
                and event.detail.get("state_at_refusal") == IntentState.ORDER_CREATED.value
                and event.detail.get("guard_contract") == "core-final-send-v1"
                for event in events
            )
    return result


def settled_zero_for_config(
    config: CoreConfig, session: Session, intent: OrderIntent, *, cutoff: datetime
) -> BrokerEvent | None:
    """Canonical zero-fill closure is operational evidence, never a learning sample."""
    from quantbot.paper_core.outcomes import _source_id, quarantined_lots, terminal_evidence

    repo = StorageRepository(session)
    if intent.strategy_id != config.owner or intent.state not in {
        IntentState.CANCELLED,
        IntentState.REJECTED,
        IntentState.EXPIRED,
    }:
        return None
    links = table("core_intent_links")
    link = (
        session.execute(select(links).where(links.c.intent_id == intent.intent_id))
        .mappings()
        .one_or_none()
    )
    if link is None or link["role"] != "ENTRY" or link["lot_id"] in quarantined_lots(session):
        return None
    if (
        len(
            session.execute(select(links.c.intent_id).where(links.c.lot_id == link["lot_id"])).all()
        )
        != 1
    ):
        return None
    order = repo.get_broker_order_by_client_order_id(intent.client_order_id)
    if (
        order is None
        or order.filled_quantity != 0
        or repo.list_fills(broker_order_id=order.broker_order_id)
    ):
        return None
    try:
        _assert_ack_matches_intent(intent, order)
    except BrokerProtocolError:
        return None
    identity = terminal_evidence(session, order, cutoff=cutoff)
    if identity is None:
        return None
    payload = session.execute(
        select(table("core_broker_event_sources").c.payload).where(
            table("core_broker_event_sources").c.id == identity
        )
    ).scalar_one()
    event = BrokerEvent.model_validate_json(payload)
    if (
        _source_id(event) != identity
        or event.account_id != config.account_id
        or event.evidence_lane != config.evidence_lane
        or event.release_id != config.release_id
        or event.client_order_id != intent.client_order_id
        or event.correction_of is not None
        or event.activity_watermark is None
        or event.activity_watermark > event.recorded_at_utc
        or (order.submitted_at is not None and event.activity_watermark < order.submitted_at)
        or (
            config.evidence_lane == "BROKER_PAPER"
            and (
                event.provider != "alpaca"
                or event.raw_digest != content_id(order)
                or event.endpoint != "https://paper-api.alpaca.markets/v2/orders"
            )
        )
    ):
        return None
    return event
