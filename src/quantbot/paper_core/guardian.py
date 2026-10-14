"""Independent fixture guardian: fill ownership, one reserved exit, and reconciled flat."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, text

from quantbot.domain import IntentState, OrderIntent, OrderSide, OrderType, TimeInForce
from quantbot.paper_core.contracts import CycleReceipt, GuardianReceipt, ProtectionPlan, RoundTrip
from quantbot.paper_core.runtime import require_synthetic
from quantbot.paper_core.simulation import SyntheticBroker
from quantbot.paper_core.store import CoreStore, certify_inventory, put, table
from quantbot.research.manifest import canonical_result_json, content_id
from quantbot.risk import OrderPurpose
from quantbot.storage import StateConflictError, StorageRepository


async def synchronize(store: CoreStore, broker: SyntheticBroker, *, now: datetime) -> str:
    require_synthetic(store, broker)
    # Import canonical provider IDs only after acknowledgement established order ownership.
    with store.database.transaction() as session:
        repo = StorageRepository(session)
        for fill in await broker.get_fills():
            repo.record_fill(fill)
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
        # M1 covers a single full fill; partial/cancel/unknown recovery matrix belongs to M2.
        if intent.state is not IntentState.FILLED or order is None:
            continue
        existing_exit = next(
            (
                link
                for link in store.links()
                if link["lot_id"] == entry["lot_id"] and link["role"] == "EXIT"
            ),
            None,
        )
        if existing_exit is not None:
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
            if session.execute(
                select(links.c.intent_id).where(
                    links.c.lot_id == entry["lot_id"], links.c.role == "EXIT"
                )
            ).first():
                continue
            digest = content_id(
                {"lot_id": entry["lot_id"], "protection_id": plan.id, "role": "EXIT"}
            )
            exit_intent = OrderIntent(
                intent_id="core-exit-" + digest,
                client_order_id="qb-" + digest[:40],
                strategy_id=store.config.owner,
                signal_date=now.date(),
                symbol=intent.symbol,
                side=OrderSide.SELL,
                quantity=owned,
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
        with store.database.transaction() as session:
            repo = StorageRepository(session)
            exit_order = repo.get_broker_order_by_client_order_id(exit_intent.client_order_id)
            assert exit_order is not None
            exit_fills = tuple(repo.list_fills(broker_order_id=exit_order.broker_order_id))
            quantity = owned - sum((f.quantity for f in exit_fills), Decimal(0))
            if quantity != 0 or exit_order.status != "filled":
                continue
            gross = sum((f.price * f.quantity for f in exit_fills), Decimal(0)) - sum(
                (f.price * f.quantity for f in entry_fills), Decimal(0)
            )
            trip = RoundTrip(
                recorded_at_utc=now,
                lot_id=entry["lot_id"],
                decision_id=entry["decision_id"],
                entry_fill_ids=tuple(f.fill_id for f in entry_fills),
                exit_fill_ids=tuple(f.fill_id for f in exit_fills),
                terminal_order_ids=(order.broker_order_id, exit_order.broker_order_id),
                protection_id=plan.id,
                reconciliation_id=proof_id,
                open_quantity=quantity,
                gross_pnl=gross,
                source_refs=tuple(f.fill_id for f in (*entry_fills, *exit_fills)),
            )
            put(session, "core_round_trips", entry["lot_id"], trip)
        exits.append(exit_intent.intent_id)
    receipt = CycleReceipt(recorded_at_utc=now, exit_intent_ids=tuple(exits), proof_id=proof_id)
    store.record("core_cycle_receipts", receipt)
    return receipt
