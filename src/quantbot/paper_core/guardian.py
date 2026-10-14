"""Independent fixture guardian: fill ownership, one reserved exit, and reconciled flat."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, text

from quantbot.domain import IntentState, OrderIntent, OrderSide, OrderType, TimeInForce
from quantbot.paper_core.contracts import CycleReceipt, GuardianReceipt, ProtectionPlan
from quantbot.paper_core.runtime import require_synthetic
from quantbot.paper_core.simulation import SyntheticBroker
from quantbot.paper_core.store import CoreStore, certify_inventory, table
from quantbot.research.manifest import canonical_result_json, content_id
from quantbot.risk import OrderPurpose
from quantbot.storage import StateConflictError, StorageRepository


async def synchronize(store: CoreStore, broker: SyntheticBroker, *, now: datetime) -> str:
    require_synthetic(store, broker)
    from quantbot.paper_core.outcomes import recover_intents

    await recover_intents(store, broker)
    account = await broker.get_account()
    positions = await broker.get_positions()
    proof = store.proof(
        account.account,
        tuple((p.symbol, p.quantity) for p in positions),
        now=now,
        positions=positions,
        acquisition_id=content_id(
            {
                "at": now,
                "provider": broker.provider,
                "positions": positions,
                "account": account.account,
            }
        ),
    )
    certify_inventory(store, proof)
    return proof.id


def acknowledge_handoffs(store: CoreStore, *, now: datetime) -> GuardianReceipt:
    plans = store.documents("core_protection_plans")
    receipt = GuardianReceipt(
        recorded_at_utc=now,
        owner=store.config.owner,
        generation=store.config.generation,
        valid_until=now + timedelta(seconds=120),
        protection_ids=tuple(content_id(p) for p in plans),
        source_refs=tuple(content_id(p) for p in plans),
    )
    store.record("core_guardian_receipts", receipt)
    return receipt


async def guardian_cycle(
    store: CoreStore, broker: SyntheticBroker, *, now: datetime
) -> CycleReceipt:
    require_synthetic(store, broker)
    acknowledge_handoffs(store, now=now)
    try:
        proof_id = await synchronize(store, broker, now=now)
    except StateConflictError:
        receipt = CycleReceipt(recorded_at_utc=now, reasons=("INVENTORY_MISMATCH",))
        store.record("core_cycle_receipts", receipt)
        return receipt
    exits = []
    for entry in [link for link in store.links() if link["role"] == "ENTRY"]:
        plan = store.read("core_protection_plans", entry["protection_id"], ProtectionPlan)
        with store.database.transaction() as session:
            repo = StorageRepository(session)
            intent = repo.get_order_intent(entry["intent_id"])
            assert intent is not None
            order = repo.get_broker_order_by_client_order_id(intent.client_order_id)
            entry_fills = (
                ()
                if order is None
                else tuple(repo.list_fills(broker_order_id=order.broker_order_id))
            )
        if not entry_fills:
            continue
        if order is None:
            continue
        owned = sum((f.quantity for f in entry_fills), Decimal(0))
        average = sum((f.quantity * f.price for f in entry_fills), Decimal(0)) / owned
        mark = broker.mark(intent.symbol)
        stop = (
            mark is not None
            and 0 <= (now - mark[1]).total_seconds() <= 120
            and mark[0] <= average * Decimal("0.99")
        )
        if now < plan.deadline and not stop:
            continue
        links = table("core_intent_links")
        with store.database.transaction() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            from quantbot.paper_core.outcomes import terminal_evidence

            repo = StorageRepository(session)
            # Re-derive under the writer lock: stop/deadline contenders share
            # reservations in the existing outbox, including canceled partials
            # until a complete activity watermark confirms final quantity.
            existing = list(
                session.execute(
                    select(links).where(links.c.lot_id == entry["lot_id"], links.c.role == "EXIT")
                ).mappings()
            )
            exited = Decimal(0)
            prepared = None
            reserved = Decimal(0)
            for link in existing:
                previous = repo.get_order_intent(link["intent_id"])
                assert previous is not None
                previous_order = repo.get_broker_order_by_client_order_id(previous.client_order_id)
                filled = (
                    Decimal(0)
                    if previous_order is None
                    else sum(
                        (
                            f.quantity
                            for f in repo.list_fills(broker_order_id=previous_order.broker_order_id)
                        ),
                        Decimal(0),
                    )
                )
                exited += filled
                if previous_order is None and previous.state in {
                    IntentState.RISK_APPROVED,
                    IntentState.ORDER_CREATED,
                }:
                    prepared = previous
                if (
                    previous_order is None
                    or terminal_evidence(session, previous_order, cutoff=now) is None
                ):
                    reserved += previous.quantity - filled
            remaining = owned - exited - reserved
            if prepared is not None:
                exit_intent = prepared
            elif remaining <= 0:
                continue
            else:
                digest = content_id(
                    {
                        "lot_id": entry["lot_id"],
                        "protection_id": plan.id,
                        "role": "EXIT",
                        "entry_fills": tuple(f.fill_id for f in entry_fills),
                        "previous_exits": tuple(sorted(link["intent_id"] for link in existing)),
                    }
                )
                exit_intent = OrderIntent(
                    intent_id="core-exit-" + digest,
                    client_order_id="qb-" + digest[:40],
                    strategy_id=store.config.owner,
                    signal_date=now.date(),
                    symbol=intent.symbol,
                    side=OrderSide.SELL,
                    quantity=remaining,
                    order_type=OrderType.MARKET,
                    time_in_force=TimeInForce.DAY,
                    created_at=now,
                )
                StorageRepository(session).create_order_intent(exit_intent)
                session.execute(
                    links.insert().values(
                        intent_id=exit_intent.intent_id,
                        decision_id=entry["decision_id"],
                        protection_id=plan.id,
                        observation_id=entry["observation_id"],
                        role="EXIT",
                        lot_id=entry["lot_id"],
                        payload=canonical_result_json(exit_intent),
                    )
                )
        from quantbot.paper_core.runtime import coordinator

        await coordinator(store, broker).submit_durable(
            exit_intent,
            purpose=OrderPurpose.STRATEGY_EXIT,
            market_eligible=True,
            drawdown=None,
            risk_approved=None,
        )
        proof_id = await synchronize(store, broker, now=now)
        exits.append(exit_intent.intent_id)
    from quantbot.paper_core.outcomes import process_outcomes

    process_outcomes(store, cutoff=now)
    receipt = CycleReceipt(recorded_at_utc=now, exit_intent_ids=tuple(exits), proof_id=proof_id)
    store.record("core_cycle_receipts", receipt)
    return receipt
