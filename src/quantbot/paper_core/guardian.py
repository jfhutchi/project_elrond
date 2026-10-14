"""Independent fixture guardian: fill ownership, one reserved exit, and reconciled flat."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, text

from quantbot.brokers.base import BrokerProtocolError, BrokerRequestError, BrokerTransportError
from quantbot.domain import IntentState, OrderIntent, OrderSide, OrderType, TimeInForce
from quantbot.execution.coordinator import OrderSubmissionBlocked
from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
from quantbot.paper_core.contracts import CycleReceipt, Decision, GuardianReceipt, ProtectionPlan
from quantbot.paper_core.health import active_health, report
from quantbot.paper_core.runtime import require_runtime_broker
from quantbot.paper_core.simulation import SyntheticBroker
from quantbot.paper_core.store import CoreStore, certify_inventory, latest_certification, table
from quantbot.paper_core.time_evidence import (
    TimeEvidence,
    acquire_time_evidence,
    evidence_objections,
    exit_due,
    provider_interval,
)
from quantbot.research.manifest import canonical_result_json, content_id
from quantbot.risk import OrderPurpose
from quantbot.storage import StateConflictError, StorageRepository


async def synchronize(
    store: CoreStore, broker: SyntheticBroker | AlpacaCoreBroker, *, now: datetime
) -> str:
    require_runtime_broker(store, broker)
    from quantbot.paper_core.outcomes import recover_intents

    if store.config.clock_profile == "PAPER_TIME_V2":
        start = store.clocks.sample()[1]
        async with asyncio.timeout(5):
            await recover_intents(store, broker)
            account = await broker.get_account()
            positions = await broker.get_positions()
            second_account = await broker.get_account()
            second_positions = await broker.get_positions()
        if (
            store.clocks.sample()[1] - start > 5
            or account != second_account
            or positions != second_positions
        ):
            report(
                store,
                role="GUARDIAN",
                state="RETRYABLE_FAILURE",
                reasons=("INVENTORY_ACQUISITION_UNSTABLE",),
                now=now,
            )
            raise TimeoutError("INVENTORY_ACQUISITION_UNSTABLE")
        # Acquisitions finish outside the writer lock; derive a fresh cutoff afterward.
        now = store.clocks.sample()[0]
    else:
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
    try:
        certify_inventory(store, proof)
    except StateConflictError:
        if store.config.clock_profile == "PAPER_TIME_V2":
            with store.database.transaction() as session:
                latest = latest_certification(session)
                quantities, cash, fills = store.ledger(session)
            if (
                latest is not None
                and proof.generation != latest[0] + 1
                or tuple(sorted(quantities.items())) != proof.expected_quantities
                or cash != proof.expected_cash
                or fills != proof.fill_ids
            ):
                report(
                    store,
                    role="GUARDIAN",
                    state="RETRYABLE_FAILURE",
                    reasons=("RECONCILIATION_AUTHORITY_CHANGED",),
                    now=now,
                    evidence_ids=(proof.id,),
                )
                raise TimeoutError("RECONCILIATION_AUTHORITY_CHANGED") from None
            mismatched = {
                s
                for s in set(dict(proof.actual_quantities)) | set(dict(proof.expected_quantities))
                if dict(proof.actual_quantities).get(s, 0)
                != dict(proof.expected_quantities).get(s, 0)
            }
            if proof.account.account_id != store.config.account_id or (
                proof.cash_complete and proof.account.cash != proof.expected_cash
            ):
                report(
                    store,
                    role="GUARDIAN",
                    state="INTEGRITY_FAILURE",
                    scope="ACCOUNT",
                    scope_id=store.config.account_id,
                    reasons=("INVENTORY_CASH_OR_IDENTITY_CONTRADICTION",),
                    now=now,
                    evidence_ids=(proof.id,),
                    protection_blocked=proof.account.account_id != store.config.account_id,
                )
            else:
                for symbol in mismatched or {store.config.symbol}:
                    report(
                        store,
                        role="GUARDIAN",
                        state="INTEGRITY_FAILURE",
                        scope="SYMBOL",
                        scope_id=symbol,
                        reasons=("INVENTORY_OWNERSHIP_CONTRADICTION",),
                        now=now,
                        evidence_ids=(proof.id,),
                        protection_blocked=True,
                    )
        raise
    return proof.id


def acknowledge_handoffs(store: CoreStore, *, now: datetime) -> GuardianReceipt:
    if (
        store.config.evidence_lane == "BROKER_PAPER"
        or getattr(store, "runtime_role", None) is not None
    ) and getattr(store, "runtime_role", None) != "guardian":
        raise ValueError("only independent guardian runtime may acknowledge protection")
    if store.runtime_role is not None or store.config.evidence_lane == "BROKER_PAPER":
        with store.database.transaction() as session:
            links = table("core_intent_links")
            active = session.execute(
                select(links.c.protection_id).where(
                    links.c.role == "ENTRY",
                    links.c.lot_id.not_in(select(table("core_round_trips").c.id)),
                )
            ).scalars()
            plans = [
                store.read("core_protection_plans", identity, ProtectionPlan).model_dump(
                    mode="json"
                )
                for identity in active
            ]
    else:
        plans = store.documents("core_protection_plans")
    receipt = GuardianReceipt(
        recorded_at_utc=now,
        evidence_lane=store.config.evidence_lane,
        release_id=store.config.release_id,
        owner=store.config.owner,
        generation=store.config.generation,
        valid_until=now + timedelta(seconds=120),
        protection_ids=tuple(content_id(p) for p in plans),
        source_refs=tuple(content_id(p) for p in plans),
    )
    store.record("core_guardian_receipts", receipt)
    return receipt


async def guardian_cycle(
    store: CoreStore, broker: SyntheticBroker | AlpacaCoreBroker, *, now: datetime
) -> CycleReceipt:
    require_runtime_broker(store, broker)
    acknowledge_handoffs(store, now=now)
    time_evidence = None
    reasons: tuple[str, ...] = ()
    proof_id = None
    if store.config.clock_profile == "PAPER_TIME_V2":
        time_evidence = await acquire_time_evidence(broker, store.clocks)
        store.record("core_time_evidence", time_evidence)
        store.runtime_time_evidence_id = time_evidence.id
        if evidence_objections(time_evidence, store.clocks):
            reasons = time_evidence.objections or evidence_objections(time_evidence, store.clocks)
            time_evidence = None
    try:
        proof_id = await synchronize(store, broker, now=now)
    except StateConflictError:
        reasons = (*reasons, "INVENTORY_MISMATCH")
        if store.config.clock_profile != "PAPER_TIME_V2":
            receipt = CycleReceipt(
                recorded_at_utc=now,
                evidence_lane=store.config.evidence_lane,
                reasons=reasons,
                release_id=store.config.release_id,
            )
            store.record("core_cycle_receipts", receipt)
            return receipt
    except (TimeoutError, OSError, BrokerRequestError, BrokerTransportError):
        reasons = (*reasons, "INVENTORY_ACQUISITION_RETRYABLE")
        report(store, role="GUARDIAN", state="RETRYABLE_FAILURE", reasons=reasons, now=now)
    except (ValueError, BrokerProtocolError):
        reasons = (*reasons, "INVENTORY_PROTOCOL_INVALID")
        report(store, role="GUARDIAN", state="PROTOCOL_FAILURE", reasons=reasons, now=now)
    if store.config.clock_profile == "PAPER_TIME_V2":
        now = broker.now
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
        with store.database.transaction() as session:
            if any(
                h.state == "INTEGRITY_FAILURE"
                and h.protection_blocked
                and (
                    h.scope == "ACCOUNT"
                    and h.scope_id == store.config.account_id
                    or h.scope == "SYMBOL"
                    and h.scope_id == intent.symbol
                    or h.scope == "LOT"
                    and h.scope_id == entry["lot_id"]
                )
                for h in active_health(session)
            ):
                continue
        owned = sum((f.quantity for f in entry_fills), Decimal(0))
        average = sum((f.quantity * f.price for f in entry_fills), Decimal(0)) / owned
        mark = broker.mark(intent.symbol)
        stop = (
            mark is not None
            and 0 <= (now - mark[1]).total_seconds() <= 120
            and mark[0] <= average * Decimal("0.99")
        )
        due = now >= plan.deadline
        if plan.clock_profile == "PAPER_TIME_V2":
            elapsed = None
            if (
                plan.clock_process_id == store.clocks.process_id
                and plan.monotonic_anchor is not None
            ):
                elapsed = store.clocks.sample()[1] - plan.monotonic_anchor
            if time_evidence is not None and evidence_objections(time_evidence, store.clocks):
                time_evidence = None
            due = exit_due(plan, time_evidence, elapsed)
            if elapsed is not None and plan.monotonic_expiry is None:
                # Old immutable V2 plans predate persisted monotonic expiry. Use
                # their original provider bracket at the matching monotonic origin.
                decision = store.read("core_decisions", plan.decision_id, Decision)
                if decision.time_evidence_id is not None:
                    origin = store.read(
                        "core_time_evidence", decision.time_evidence_id, TimeEvidence
                    )
                    if (
                        origin.process_id == plan.clock_process_id
                        and plan.monotonic_anchor is not None
                    ):
                        provider_origin = origin.wall_anchor + timedelta(
                            seconds=origin.offset_upper
                            + plan.monotonic_anchor
                            - origin.acquired_monotonic
                        )
                        duration = max(
                            0, min(1800, (plan.deadline - provider_origin).total_seconds())
                        )
                        due = elapsed >= duration or exit_due(plan, time_evidence, None)
            if time_evidence is not None:
                # Project fresh provider time after every awaited acquisition. An
                # independent guardian never compares the trader's monotonic epoch.
                due = due or provider_interval(time_evidence, store.clocks)[1] >= plan.deadline
            # A discontinuous wall cannot stamp a stale mark current.
            stop = False
            if (
                time_evidence is not None
                and not evidence_objections(time_evidence, store.clocks)
                and mark is not None
            ):
                earliest, latest_time = provider_interval(time_evidence, store.clocks)
                stop = (
                    earliest >= mark[1]
                    and (latest_time - mark[1]).total_seconds() <= 120
                    and mark[0] <= average * Decimal("0.99")
                )
        if not due and not stop:
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
                    strategy_id=plan.owner,
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

        try:
            await coordinator(store, broker).submit_durable(
                exit_intent,
                purpose=OrderPurpose.STRATEGY_EXIT,
                market_eligible=True,
                drawdown=None,
                risk_approved=None,
            )
        except (
            TimeoutError,
            OSError,
            BrokerRequestError,
            BrokerTransportError,
            OrderSubmissionBlocked,
        ):
            if store.config.clock_profile != "PAPER_TIME_V2":
                raise
            reasons = (*reasons, "OWNED_EXIT_RETRY_PENDING")
            report(
                store,
                role="GUARDIAN",
                state="RETRYABLE_FAILURE",
                reasons=reasons,
                now=store.clocks.sample()[0],
                scope="LOT",
                scope_id=entry["lot_id"],
            )
            continue
        except BrokerProtocolError:
            reasons = (*reasons, "OWNED_EXIT_PROTOCOL_QUARANTINED")
            report(
                store,
                role="GUARDIAN",
                state="PROTOCOL_FAILURE",
                reasons=reasons,
                now=store.clocks.sample()[0],
                scope="LOT",
                scope_id=entry["lot_id"],
            )
            continue
        try:
            proof_id = await synchronize(store, broker, now=broker.now)
        except (
            TimeoutError,
            OSError,
            StateConflictError,
            BrokerRequestError,
            BrokerTransportError,
        ):
            reasons = (*reasons, "POST_EXIT_RECONCILIATION_WAIT")
        exits.append(exit_intent.intent_id)
    from quantbot.paper_core.outcomes import process_outcomes

    if store.config.clock_profile == "PAPER_TIME_V2":
        now = broker.now
    process_outcomes(store, cutoff=now)
    receipt = CycleReceipt(
        recorded_at_utc=now,
        evidence_lane=store.config.evidence_lane,
        release_id=store.config.release_id,
        exit_intent_ids=tuple(exits),
        proof_id=proof_id,
        reasons=reasons,
    )
    store.record("core_cycle_receipts", receipt)
    return receipt
