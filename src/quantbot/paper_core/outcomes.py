"""Replay-safe canonical broker evidence, owned completion and economic revisions.

No fresh market observation, market-open gate, entry eligibility or POST is used
by the outcome/recovery interfaces. All provider operations are explicit synthetic
reads until a separately commissioned adapter supplies equivalent evidence.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from quantbot.domain import BrokerOrder, Fill, IntentState, OrderIntent, OrderSide
from quantbot.execution.coordinator import _ack_terminal_target, _assert_ack_matches_intent
from quantbot.execution.recovery import is_open_order
from quantbot.paper_core.contracts import (
    BrokerEvent,
    CycleReceipt,
    Decision,
    GuardianReceipt,
    Observation,
    OutcomeBatch,
    ProtectionPlan,
    ReconciliationProof,
    RoundTrip,
)
from quantbot.paper_core.costs import actual_fee, current_costs, record_cost
from quantbot.paper_core.runtime import require_synthetic
from quantbot.paper_core.simulation import SyntheticBroker
from quantbot.paper_core.store import CoreStore, put, table
from quantbot.research.manifest import canonical_result_json, content_id
from quantbot.storage import StateConflictError, StorageRepository
from quantbot.storage.database import encode_utc


class OutcomeIncomplete(ValueError):
    """Owned completion evidence is not yet sufficient for a round trip."""


def _semantic(event: BrokerEvent) -> str:
    payload = event.model_dump(mode="python")
    payload.pop("recorded_at_utc")
    # Poll time is not provider order event identity. First availability is retained.
    if event.order is not None:
        payload.pop("occurred_at")
        # A later identical complete poll doesn't revise first availability.
        payload.pop("activity_watermark")
    return canonical_result_json(payload)


def _source_id(event: BrokerEvent) -> str:
    return content_id(
        {
            "account": event.account_id,
            "provider": event.provider,
            "endpoint": event.endpoint,
            "event": event.event_id,
        }
    )


def _adopt(repo: StorageRepository, intent: OrderIntent, order: BrokerOrder) -> None:
    _assert_ack_matches_intent(intent, order)
    old = repo.get_broker_order_by_client_order_id(intent.client_order_id)
    if old is None:
        repo.save_broker_order(order)
    else:
        if order.filled_quantity < old.filled_quantity:
            raise StateConflictError("order filled quantity regressed")
        repo.update_broker_order_lifecycle(order)
    if intent.state in {IntentState.SUBMITTING, IntentState.SUBMISSION_UNKNOWN}:
        intent = repo.transition_order_intent(
            intent.intent_id, IntentState.BROKER_ACCEPTED, expected_current=intent.state
        )
    target = _ack_terminal_target(order)
    if target is not None and intent.state not in {
        IntentState.FILLED,
        IntentState.CANCELLED,
        IntentState.EXPIRED,
        IntentState.REJECTED,
    }:
        repo.transition_order_intent(intent.intent_id, target, expected_current=intent.state)


def import_broker_events(store: CoreStore, events: tuple[BrokerEvent, ...]) -> tuple[str, ...]:
    try:
        return _import_events(store, events)
    except StateConflictError as error:
        # Roll back all business rows first, then preserve the rejected evidence.
        # Incomplete acquisition is not an account contradiction or a global stop.
        conflict = any(
            word in str(error)
            for word in (
                "conflicting",
                "conflicts",
                "regressed",
                "contradicts",
                "exceed",
                "mismatch",
            )
        )
        identity = content_id({"events": events, "reason": str(error)})
        with store.database.transaction() as session:
            repo = StorageRepository(session)
            clients = {event.client_order_id for event in events}
            lot_ids = []
            for link in session.execute(select(table("core_intent_links"))).mappings():
                intent = repo.get_order_intent(link["intent_id"])
                if intent is not None and intent.client_order_id in clients:
                    lot_ids.append(str(link["lot_id"]))
            repo.create_incident(
                "core-evidence-" + identity,
                severity="ERROR",
                kind="CORE_EVIDENCE_CONFLICT" if conflict else "CORE_EVIDENCE_REFUSED",
                message=str(error),
                occurred_at=max(e.recorded_at_utc for e in events),
                detail={
                    "source_events": [e.model_dump(mode="json") for e in events],
                    "lot_ids": sorted(set(lot_ids)),
                    "evidence_lane": store.config.evidence_lane,
                },
            )
            if conflict:
                pointer = table("core_outcome_index")
                session.execute(
                    update(pointer)
                    .where(pointer.c.lot_id.in_(lot_ids))
                    .values(status="INTEGRITY_FAILURE")
                )
        raise


def quarantined_lots(session: Session) -> set[str]:
    result: set[str] = set()
    for incident in StorageRepository(session).list_incidents(unresolved_only=True):
        lots = incident.detail.get("lot_ids", [])
        if incident.kind == "CORE_EVIDENCE_CONFLICT" and isinstance(lots, list):
            result.update(str(lot) for lot in lots)
    return result


def _import_events(store: CoreStore, events: tuple[BrokerEvent, ...]) -> tuple[str, ...]:
    """Commit canonical events, fees and source cursors together; conflicting replay rolls back."""
    imported: list[str] = []
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        repo = StorageRepository(session)
        links = {
            link["intent_id"]: link
            for link in session.execute(select(table("core_intent_links"))).mappings()
        }
        ordered = sorted(events, key=lambda e: 0 if e.order else 1 if e.fill else 2)
        for supplied in ordered:
            event = BrokerEvent.model_validate(supplied.model_dump())
            if (
                event.account_id != store.config.account_id
                or event.evidence_lane != store.config.evidence_lane
                or event.release_id != store.config.release_id
                or event.correction_of is not None
            ):
                raise StateConflictError("source binding or correction authority mismatch")
            identity = _source_id(event)
            sources = table("core_broker_event_sources")
            previous = session.execute(
                select(sources.c.payload).where(sources.c.id == identity)
            ).scalar_one_or_none()
            if previous is not None:
                old = BrokerEvent.model_validate_json(previous)
                if _semantic(old) != _semantic(event):
                    raise StateConflictError("conflicting source event replay")
                continue
            intent = next(
                (
                    i
                    for i in (repo.get_order_intent(identity) for identity in links)
                    if i is not None and i.client_order_id == event.client_order_id
                ),
                None,
            )
            if intent is None or intent.intent_id not in links:
                raise StateConflictError("event has no accepted owned intent")
            if event.order:
                _adopt(repo, intent, event.order)
            if event.fill:
                order = repo.get_broker_order_by_client_order_id(intent.client_order_id)
                if (
                    order is None
                    or event.fill.broker_order_id != order.broker_order_id
                    or event.fill.symbol != intent.symbol
                    or event.fill.side is not intent.side
                    or event.fill.occurred_at < order.submitted_at
                ):
                    raise StateConflictError("fill ownership/chronology mismatch")
                repo.record_fill(event.fill)
            if event.cost:
                fill = repo.get_fill(event.cost.fill_id)
                order = repo.get_broker_order_by_client_order_id(intent.client_order_id)
                if fill is None or order is None or fill.broker_order_id != order.broker_order_id:
                    raise StateConflictError("cost has no exact owned fill")
                if event.cost.recorded_at_utc != event.recorded_at_utc:
                    raise StateConflictError("cost availability does not match source receipt")
                record_cost(session, event.cost, fill)
            if event.cash_activity:
                activity = event.cash_activity
                fills = repo.list_fills()
                movements = dict(activity.movements)
                if (
                    not activity.complete
                    or activity.account_id != store.config.account_id
                    or activity.evidence_lane != store.config.evidence_lane
                    or activity.release_id != store.config.release_id
                    or len(movements) != len(activity.movements)
                    or set(movements) != {f.fill_id for f in fills}
                    or len(activity.activity_ids) != len(fills)
                    or len(set(activity.activity_ids)) != len(activity.activity_ids)
                    or activity.watermark > activity.recorded_at_utc
                    or any(activity.watermark < f.occurred_at for f in fills)
                    or activity.recorded_at_utc != event.recorded_at_utc
                ):
                    raise StateConflictError(
                        "cash activity complete ownership watermark unavailable"
                    )
                for fill in fills:
                    gross = fill.quantity * fill.price
                    amount = movements[fill.fill_id]
                    if (fill.side is OrderSide.BUY and amount > -gross) or (
                        fill.side is OrderSide.SELL and (amount > gross or amount < 0)
                    ):
                        raise StateConflictError(
                            "cash movement contradicts fill direction/notional"
                        )
                existing = session.execute(
                    select(table("core_cash_activities").c.payload)
                ).scalars()
                for encoded in existing:
                    from quantbot.paper_core.contracts import CashActivity

                    old_activity = CashActivity.model_validate_json(encoded)
                    if (
                        dict(old_activity.movements).keys() == movements.keys()
                        and old_activity.movements != activity.movements
                    ):
                        raise StateConflictError(
                            "conflicting cash activity; explicit correction required"
                        )
                put(session, "core_cash_activities", activity.id, activity)
            put(session, "core_broker_event_sources", identity, event)
            cursor = table("core_source_cursors")
            source = content_id(
                {
                    "provider": event.provider,
                    "account": event.account_id,
                    "endpoint": event.endpoint,
                }
            )
            current = session.execute(
                select(cursor.c.available_at).where(cursor.c.source_id == source)
            ).scalar_one_or_none()
            available = encode_utc(event.recorded_at_utc)
            if current is None:
                session.execute(
                    cursor.insert().values(
                        source_id=source, available_at=available, event_id=identity
                    )
                )
            elif available > current:
                session.execute(
                    update(cursor)
                    .where(cursor.c.source_id == source)
                    .values(available_at=available, event_id=identity)
                )
            imported.append(identity)
        for order in repo.list_broker_orders():
            total = sum(
                (f.quantity for f in repo.list_fills(broker_order_id=order.broker_order_id)),
                Decimal(0),
            )
            if total > order.filled_quantity:
                raise StateConflictError("canonical fills exceed broker acknowledged quantity")
    return tuple(imported)


def terminal_evidence(session: Session, order: BrokerOrder, *, cutoff: datetime) -> str | None:
    if is_open_order(order):
        return None
    fills = StorageRepository(session).list_fills(broker_order_id=order.broker_order_id)
    if sum((f.quantity for f in fills), Decimal(0)) != order.filled_quantity:
        return None
    candidates = []
    for row in session.execute(select(table("core_broker_event_sources"))).mappings():
        event = BrokerEvent.model_validate_json(row["payload"])
        if (
            event.order == order
            and event.recorded_at_utc <= cutoff
            and event.activity_complete
            and set(event.activity_fill_ids) == {f.fill_id for f in fills}
            and len(event.activity_fill_ids) == len(fills)
            and event.activity_watermark is not None
            and all(f.occurred_at <= event.activity_watermark for f in fills)
        ):
            candidates.append((event.recorded_at_utc, str(row["id"])))
    return min(candidates)[1] if candidates else None


def _resolve(store: CoreStore, session: Session, lot_id: str, as_of: datetime) -> RoundTrip:
    if lot_id in quarantined_lots(session):
        raise OutcomeIncomplete("SOURCE_EVIDENCE_QUARANTINED")
    repo = StorageRepository(session)
    links = list(
        session.execute(
            select(table("core_intent_links")).where(table("core_intent_links").c.lot_id == lot_id)
        ).mappings()
    )
    entries = [link for link in links if link["role"] == "ENTRY"]
    if len(entries) != 1:
        raise OutcomeIncomplete("ENTRY_OWNERSHIP_UNAVAILABLE")
    entry = entries[0]
    buys: list[Fill] = []
    sells: list[Fill] = []
    terminal_ids: list[str] = []
    source_ids: list[str] = []
    for link in sorted(links, key=lambda row: row["intent_id"]):
        intent = repo.get_order_intent(link["intent_id"])
        assert intent is not None
        order = repo.get_broker_order_by_client_order_id(intent.client_order_id)
        if order is None:
            raise OutcomeIncomplete("ORDER_UNAVAILABLE")
        terminal_id = terminal_evidence(session, order, cutoff=as_of)
        if terminal_id is None:
            raise OutcomeIncomplete("TERMINAL_ACTIVITY_WATERMARK_UNAVAILABLE")
        fills = repo.list_fills(broker_order_id=order.broker_order_id)
        if any(f.occurred_at > as_of for f in fills):
            raise OutcomeIncomplete("FILL_NOT_AVAILABLE")
        (buys if link["role"] == "ENTRY" else sells).extend(fills)
        terminal_ids.append(order.broker_order_id)
        source_ids.append(terminal_id)
    quantity = sum((f.quantity for f in buys), Decimal(0)) - sum(
        (f.quantity for f in sells), Decimal(0)
    )
    if not buys or not sells or quantity != 0:
        raise OutcomeIncomplete("NONEMPTY_OWNED_FLAT_UNAVAILABLE")
    inventory = Decimal(0)
    for fill in sorted(
        (*buys, *sells),
        key=lambda f: (f.occurred_at, 0 if f.side is OrderSide.BUY else 1, f.fill_id),
    ):
        inventory += fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
        if inventory < 0:
            raise StateConflictError("owned exit exceeds chronological owned entry")
    proofs = []
    fill_ids = {f.fill_id for f in (*buys, *sells)}
    for name in ("core_inventory_certifications", "core_quantity_certifications"):
        certified = select(table(name).c.proof_id)
        if name == "core_inventory_certifications":
            certified = certified.where(
                table(name).c.snapshot_id.not_in(
                    select(table("core_cash_authority_revocations").c.snapshot_id)
                )
            )
        for proof_id in session.execute(certified).scalars():
            proof = ReconciliationProof.model_validate_json(
                session.execute(
                    select(table("core_reconciliation_proofs").c.payload).where(
                        table("core_reconciliation_proofs").c.id == proof_id
                    )
                ).scalar_one()
            )
            if (
                proof.recorded_at_utc <= as_of
                and proof.complete
                and fill_ids <= set(proof.fill_ids)
                and proof.actual_quantities == proof.expected_quantities
            ):
                proofs.append(proof)
    if not proofs:
        raise OutcomeIncomplete("OWNED_FLAT_RECONCILIATION_UNAVAILABLE")
    proof = min(proofs, key=lambda p: (p.recorded_at_utc, p.generation))
    decision = Decision.model_validate_json(
        session.execute(
            select(table("core_decisions").c.payload).where(
                table("core_decisions").c.id == entry["decision_id"]
            )
        ).scalar_one()
    )
    source_start = decision.source_start
    if source_start is None:
        # Old decisions did not carry this metadata, but their immutable
        # observation is historical evidence, never fresh entry data.
        observation_payload = session.execute(
            select(table("core_observations").c.payload).where(
                table("core_observations").c.id == decision.observation_id
            )
        ).scalar_one_or_none()
        if observation_payload is not None:
            source_start = Observation.model_validate_json(observation_payload).source_start
    plan = ProtectionPlan.model_validate_json(
        session.execute(
            select(table("core_protection_plans").c.payload).where(
                table("core_protection_plans").c.id == entry["protection_id"]
            )
        ).scalar_one()
    )
    if plan.decision_id != decision.id or plan.owner != store.config.owner:
        raise StateConflictError("protection authority mismatch")
    receipts = [
        GuardianReceipt.model_validate_json(p)
        for p in session.execute(select(table("core_guardian_receipts").c.payload)).scalars()
    ]
    handoffs = [
        r
        for r in receipts
        if plan.id in r.protection_ids
        and r.owner == plan.owner
        and r.generation == plan.generation
        and r.recorded_at_utc <= min(f.occurred_at for f in buys) < r.valid_until
    ]
    if not handoffs:
        raise OutcomeIncomplete("PRESEND_GUARDIAN_HANDOFF_UNAVAILABLE")
    handoff = min(handoffs, key=lambda r: (r.recorded_at_utc, r.id))
    costs = current_costs(session, cutoff=as_of)
    allocated = [
        costs[(f.fill_id, component)]
        for f in (*buys, *sells)
        for component in ("FEE", "MODELED_SLIPPAGE")
        if (f.fill_id, component) in costs
    ]
    fees = [actual_fee(f, costs) for f in (*buys, *sells)]
    actual = (
        None
        if any(f is None for f in fees)
        else sum((f for f in fees if f is not None), Decimal(0))
    )
    gross = sum((f.price * f.quantity for f in sells), Decimal(0)) - sum(
        (f.price * f.quantity for f in buys), Decimal(0)
    )
    modeled = [i.amount for i in allocated if i.source == "MODELED"]
    modeled_costs = sum((m for m in modeled if m is not None), Decimal(0)) if modeled else None
    sources = tuple(
        sorted((*source_ids, *fill_ids, *(i.id for i in allocated), plan.id, handoff.id, proof.id))
    )
    ordered_fills: tuple[Fill, ...] = tuple(sorted([*buys, *sells], key=lambda f: f.fill_id))
    state_hash = content_id(
        {"resolver": "owned-executed-net-return-v1", "sources": sources, "fills": ordered_fills}
    )
    revisions = [
        RoundTrip.model_validate_json(p)
        for p in session.execute(select(table("core_outcome_revisions").c.payload)).scalars()
    ]
    revisions = [r for r in revisions if r.lot_id == lot_id]
    identical = next((r for r in revisions if r.source_state_hash == state_hash), None)
    if identical:
        return identical
    previous = max(revisions, key=lambda r: r.revision, default=None)
    return RoundTrip(
        recorded_at_utc=as_of,
        release_id=store.config.release_id,
        source_refs=sources,
        lot_id=lot_id,
        decision_id=decision.id,
        entry_fill_ids=tuple(sorted(f.fill_id for f in buys)),
        exit_fill_ids=tuple(sorted(f.fill_id for f in sells)),
        terminal_order_ids=tuple(sorted(terminal_ids)),
        protection_id=plan.id,
        reconciliation_id=proof.id,
        open_quantity=quantity,
        gross_pnl=gross,
        net_pnl=None if actual is None else gross - actual,
        actual_fees=actual,
        status="COSTS_PENDING" if actual is None else "COMPLETE",
        cost_reason="FEE_UNAVAILABLE" if actual is None else "PROVEN_ACTUAL_FEES",
        cost_item_ids=tuple(sorted(i.id for i in allocated)),
        modeled_costs=modeled_costs,
        modeled_net_pnl=None if modeled_costs is None else gross - modeled_costs,
        source_state_hash=state_hash,
        revision=1 if previous is None else previous.revision + 1,
        supersedes=None if previous is None else previous.id,
        policy_id=decision.policy_id,
        entry_notional=sum((f.quantity * f.price for f in buys), Decimal(0)),
        source_start=source_start or min(f.occurred_at for f in buys),
        dataset="EXPLICIT_SYNTHETIC" if source_start else "LEGACY_WINDOW_UNAVAILABLE",
        source_end=max(f.occurred_at for f in sells),
        available_at=as_of,
    )


def reconcile_round_trip(store: CoreStore, lot_id: str, *, as_of: datetime) -> RoundTrip:
    with store.database.transaction() as session:
        return _resolve(store, session, lot_id, as_of)


def process_outcomes(store: CoreStore, *, cutoff: datetime) -> OutcomeBatch:
    outcomes, reasons = [], []
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        lots = (
            session.execute(
                select(table("core_intent_links").c.lot_id).where(
                    table("core_intent_links").c.role == "ENTRY"
                )
            )
            .scalars()
            .all()
        )
        for lot in lots:
            try:
                trip = _resolve(store, session, lot, cutoff)
            except OutcomeIncomplete as error:
                reasons.append(f"{lot}:{error}")
                continue
            pointer = table("core_outcome_index")
            current = session.execute(
                select(pointer.c.outcome_id).where(pointer.c.lot_id == lot)
            ).scalar_one_or_none()
            if current == trip.id:
                continue
            if current is not None and trip.supersedes != current:
                # Historical cutoff cannot roll the current outcome backwards.
                reasons.append(f"{lot}:HISTORICAL_REVISION")
                continue
            put(session, "core_outcome_revisions", trip.id, trip)
            target = table("core_round_trips")
            exists = session.execute(select(target.c.id).where(target.c.id == lot)).first()
            if exists:
                session.execute(
                    update(target)
                    .where(target.c.id == lot)
                    .values(payload=canonical_result_json(trip))
                )
            else:
                put(session, "core_round_trips", lot, trip)
            assert trip.source_start and trip.source_end and trip.available_at
            fields = dict(
                outcome_id=trip.id,
                available_at=encode_utc(trip.available_at),
                source_start=encode_utc(trip.source_start),
                source_end=encode_utc(trip.source_end),
                dataset=trip.dataset,
                policy_id=trip.policy_id,
                evidence_lane=trip.evidence_lane,
                status=trip.status,
            )
            if current:
                session.execute(update(pointer).where(pointer.c.lot_id == lot).values(**fields))
            else:
                session.execute(pointer.insert().values(lot_id=lot, **fields))
            outcomes.append(trip)
    return OutcomeBatch(recorded_at_utc=cutoff, outcomes=tuple(outcomes), reasons=tuple(reasons))


async def recover_intents(store: CoreStore, broker: SyntheticBroker) -> CycleReceipt:
    """Read the original client IDs, import evidence, and never POST an unknown intent."""
    require_synthetic(store, broker)
    reasons = []
    with store.database.transaction() as session:
        intents = [
            StorageRepository(session).get_order_intent(link["intent_id"]) for link in store.links()
        ]
    events = []
    fills = await broker.get_fills()
    watermark = await broker.get_activity_watermark()
    for intent in intents:
        assert intent is not None
        order = await broker.get_order_by_client_id(intent.client_order_id, expected=intent)
        if order is None:
            if intent.state in {IntentState.SUBMITTING, IntentState.SUBMISSION_UNKNOWN}:
                with store.database.transaction() as session:
                    current = StorageRepository(session).get_order_intent(intent.intent_id)
                    assert current is not None
                    if current.state is IntentState.SUBMITTING:
                        StorageRepository(session).transition_order_intent(
                            intent.intent_id,
                            IntentState.SUBMISSION_UNKNOWN,
                            expected_current=IntentState.SUBMITTING,
                        )
                reasons.append("SUBMISSION_UNKNOWN:" + intent.intent_id)
            continue
        owned_fills = tuple(f for f in fills if f.broker_order_id == order.broker_order_id)
        # Synthetic get_fills returns a complete, durable journal. Real provider
        # pagination/cancel watermark authority is deliberately not inferred here.
        events.append(
            BrokerEvent(
                recorded_at_utc=broker.now,
                occurred_at=broker.now,
                account_id=store.config.account_id,
                endpoint="synthetic/order-and-complete-activities",
                event_id=(
                    content_id(order)
                    if watermark is not None
                    else content_id({"order": order, "activity_complete": False})
                ),
                raw_digest=content_id(order),
                client_order_id=intent.client_order_id,
                order=order,
                activity_complete=watermark is not None,
                activity_watermark=watermark,
                activity_fill_ids=tuple(f.fill_id for f in owned_fills),
            )
        )
        for fill in owned_fills:
            events.append(
                BrokerEvent(
                    recorded_at_utc=broker.now,
                    occurred_at=fill.occurred_at,
                    account_id=store.config.account_id,
                    endpoint="synthetic/activities",
                    event_id=fill.fill_id,
                    raw_digest=content_id(fill),
                    client_order_id=intent.client_order_id,
                    fill=fill,
                )
            )
    import_broker_events(store, tuple(events))
    receipt = CycleReceipt(recorded_at_utc=broker.now, reasons=tuple(reasons))
    store.record("core_cycle_receipts", receipt)
    return receipt
