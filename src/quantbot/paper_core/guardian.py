"""Independent fixture guardian: fill ownership, one reserved exit, and reconciled flat."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from quantbot.brokers.base import BrokerProtocolError, BrokerRequestError, BrokerTransportError
from quantbot.domain import IntentState, OrderIntent, OrderSide, OrderType, TimeInForce
from quantbot.execution.coordinator import OrderSubmissionBlocked
from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
from quantbot.paper_core.contracts import (
    CycleReceipt,
    Decision,
    GuardianReceipt,
    ProtectionPlan,
    ReconciliationProof,
)
from quantbot.paper_core.funds import (
    PaperFundsEvidence,
    paper_balance_policy,
    stable_account_reads,
    stable_position_reads,
)
from quantbot.paper_core.health import active_health, report
from quantbot.paper_core.runtime import require_runtime_broker
from quantbot.paper_core.simulation import SyntheticBroker
from quantbot.paper_core.store import CoreStore, certify_inventory, latest_certification, put, table
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


def _capture_crossing_sends(
    store: CoreStore, session: Session, *, capture_started: datetime, now: datetime
) -> tuple[str, ...]:
    """Detect invalid acquisition ordering, without inferring a broker fill or ownership."""
    events, links = table("order_events"), table("core_intent_links")
    rows = session.execute(
        select(events.c.intent_id)
        .join(links, links.c.intent_id == events.c.intent_id)
        .where(
            events.c.event_type == "CORE_LOCKED_SEND_AUTHORIZED",
            events.c.occurred_at >= capture_started.isoformat(),
            events.c.occurred_at <= (now + timedelta(seconds=1)).isoformat(),
            links.c.role == "ENTRY",
        )
        .distinct()
    ).scalars()
    repo = StorageRepository(session)
    crossed = []
    for intent_id in rows:
        linked = (
            session.execute(select(links).where(links.c.intent_id == intent_id)).mappings().one()
        )
        intent = repo.get_order_intent(intent_id)
        if (
            intent is None
            or intent.side is not OrderSide.BUY
            or intent.strategy_id != store.config.owner
        ):
            continue
        encoded = session.execute(
            select(table("core_protection_plans").c.payload).where(
                table("core_protection_plans").c.id == linked["protection_id"]
            )
        ).scalar_one_or_none()
        if encoded is None:
            continue
        plan = ProtectionPlan.model_validate_json(encoded)
        if (
            plan.id != linked["protection_id"]
            or plan.decision_id != linked["decision_id"]
            or plan.owner != store.config.owner
            or plan.generation != store.config.generation
            or plan.clock_profile != "PAPER_TIME_V2"
        ):
            continue
        sends = [
            event
            for event in repo.list_order_events(intent_id=intent_id)
            if event.event_type == "CORE_LOCKED_SEND_AUTHORIZED"
            and capture_started <= event.occurred_at <= now
            and event.detail.get("contract") == "core-final-send-v1"
            and event.detail.get("config_id") == store.config_id
            and event.detail.get("decision_id") == linked["decision_id"]
            and event.detail.get("protection_id") == plan.id
            and event.detail.get("process_epoch") == plan.clock_process_id
        ]
        if len(sends) != 1:
            continue
        send = sends[0]
        encoded = session.execute(
            select(table("core_guardian_receipts").c.payload).where(
                table("core_guardian_receipts").c.id == send.detail.get("guardian_receipt_id")
            )
        ).scalar_one_or_none()
        if encoded is None:
            continue
        ack = GuardianReceipt.model_validate_json(encoded)
        if (
            ack.id == send.detail.get("guardian_receipt_id")
            and plan.id in ack.protection_ids
            and ack.owner == store.config.owner
            and ack.generation == store.config.generation
            and ack.release_id == store.config.release_id
            and ack.recorded_at_utc <= send.occurred_at < ack.valid_until
        ):
            crossed.append(send.event_id)
    return tuple(sorted(crossed))


def _pending_owned_activity(
    store: CoreStore,
    proof: ReconciliationProof,
    *,
    capture_started: datetime,
    now: datetime,
    activity_document_id: str | None = None,
    session: Session | None = None,
) -> bool:
    """Defer only a quantity delta explained by this poll's stable owned acknowledgements."""
    import json

    from quantbot.domain import BrokerOrder

    if proof.account.account_id != store.config.account_id:
        return False
    with store.database.transaction() if session is None else nullcontext(session) as session:
        if activity_document_id is None:
            progress = (
                session.execute(
                    select(table("core_provider_progress")).where(
                        table("core_provider_progress").c.account_id == store.config.account_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if progress is None:
                return False
            activity_document_id = progress["document_id"]
            watermark = progress["watermark"]
        else:
            watermark = None
        encoded = session.execute(
            select(table("core_provider_acquisitions").c.payload).where(
                table("core_provider_acquisitions").c.id == activity_document_id
            )
        ).scalar_one_or_none()
        if encoded is None:
            return False
        document = json.loads(encoded)
        if (
            content_id(document) != activity_document_id
            or document.get("account_id") != store.config.account_id
            or document.get("release_id") != store.config.release_id
            or document.get("endpoint") != AlpacaCoreBroker.endpoint
            or not capture_started <= datetime.fromisoformat(document["until"]) <= now
            or (
                watermark is not None
                and datetime.fromisoformat(watermark) != datetime.fromisoformat(document["until"])
            )
            or session.execute(select(table("core_fixture_baselines").c.id)).scalar_one_or_none()
            != proof.baseline_id
        ):
            return False
        repo = StorageRepository(session)
        ambiguous = set(
            session.execute(
                select(table("order_intents").c.intent_id).where(
                    table("order_intents").c.state.in_(("SUBMITTING", "SUBMISSION_UNKNOWN"))
                )
            ).scalars()
        )
        acknowledged = set()
        pending: dict[str, Decimal] = {}
        buy_basis: dict[str, Decimal] = {}
        pending_payloads = list(document.get("pending_owned_orders", ()))
        acknowledged_ids = {BrokerOrder.model_validate(p).broker_order_id for p in pending_payloads}
        crossed_sends = []
        events, intents = table("order_events"), table("order_intents")
        recent_clients = session.execute(
            select(intents.c.client_order_id)
            .join(events, events.c.intent_id == intents.c.intent_id)
            .where(
                events.c.event_type == "CORE_LOCKED_SEND_AUTHORIZED",
                events.c.occurred_at >= capture_started.isoformat(),
                events.c.occurred_at <= (now + timedelta(seconds=1)).isoformat(),
            )
            .distinct()
        ).scalars()
        for client_order_id in recent_clients:
            order = repo.get_broker_order_by_client_order_id(client_order_id)
            if (
                order is None
                or order.broker_order_id in acknowledged_ids
                or order.status != "filled"
            ):
                continue
            linked = (
                session.execute(
                    select(table("core_intent_links"))
                    .join(
                        table("order_intents"),
                        table("order_intents").c.intent_id
                        == table("core_intent_links").c.intent_id,
                    )
                    .where(
                        table("order_intents").c.client_order_id == order.client_order_id,
                        table("core_intent_links").c.role == "ENTRY",
                    )
                )
                .mappings()
                .one_or_none()
            )
            intent = repo.get_order_intent(linked["intent_id"]) if linked else None
            if (
                intent is None
                or intent.state is not IntentState.FILLED
                or intent.side is not OrderSide.BUY
            ):
                continue
            assert linked is not None
            plan_payload = session.execute(
                select(table("core_protection_plans").c.payload).where(
                    table("core_protection_plans").c.id == linked["protection_id"]
                )
            ).scalar_one()
            plan = ProtectionPlan.model_validate_json(plan_payload)
            sends = [
                e
                for e in repo.list_order_events(intent_id=intent.intent_id)
                if e.event_type == "CORE_LOCKED_SEND_AUTHORIZED"
                and capture_started <= e.occurred_at <= now
                and e.detail.get("contract") == "core-final-send-v1"
                and e.detail.get("config_id") == store.config_id
                and e.detail.get("decision_id") == linked["decision_id"]
                and e.detail.get("protection_id") == plan.id
                and e.detail.get("process_epoch") == plan.clock_process_id
            ]
            if len(sends) != 1:
                continue
            ack_id = sends[0].detail.get("guardian_receipt_id")
            ack_payload = session.execute(
                select(table("core_guardian_receipts").c.payload).where(
                    table("core_guardian_receipts").c.id == ack_id
                )
            ).scalar_one_or_none()
            if ack_payload is None:
                continue
            ack = GuardianReceipt.model_validate_json(ack_payload)
            if not (
                plan.id in ack.protection_ids
                and ack.owner == store.config.owner
                and ack.generation == store.config.generation
                and ack.release_id == store.config.release_id
                and ack.recorded_at_utc <= sends[0].occurred_at < ack.valid_until
            ):
                continue
            # This poll selected owned intents before the original POST was sent.
            # The send can precede its later activity cutoff while the fill follows
            # it. Only an exact bound send inside THIS capture explains a retry.
            # This grants only a retry: no fill, inventory, cash or exit authority.
            pending_payloads.append(order.model_dump(mode="json"))
            crossed_sends.append(sends[0].event_id)
        for payload in pending_payloads:
            order = BrokerOrder.model_validate(payload)
            linked = (
                session.execute(
                    select(table("core_intent_links"))
                    .join(
                        table("order_intents"),
                        table("order_intents").c.intent_id
                        == table("core_intent_links").c.intent_id,
                    )
                    .where(table("order_intents").c.client_order_id == order.client_order_id)
                )
                .mappings()
                .one_or_none()
            )
            intent = repo.get_order_intent(linked["intent_id"]) if linked else None
            if (
                intent is None
                or intent.strategy_id != store.config.owner
                or (intent.symbol, intent.side, intent.quantity)
                != (order.symbol, order.side, order.quantity)
                or (intent.order_type, intent.time_in_force)
                != (order.order_type, order.time_in_force)
            ):
                return False
            assert linked is not None and intent is not None
            stored_order = repo.get_broker_order_by_client_order_id(order.client_order_id)
            if stored_order != order:
                if stored_order is not None or intent.intent_id not in ambiguous:
                    return False
                # The independent GET can finish before the original POST response.
                # Keep the reservation; require the original locked send and plan.
                if not any(
                    e.event_type == "CORE_LOCKED_SEND_AUTHORIZED"
                    and e.occurred_at <= now
                    and e.detail.get("contract") == "core-final-send-v1"
                    and e.detail.get("config_id") == store.config_id
                    and e.detail.get("decision_id") == linked["decision_id"]
                    and e.detail.get("protection_id") == linked["protection_id"]
                    for e in repo.list_order_events(intent_id=intent.intent_id)
                ):
                    return False
            acknowledged.add(intent.intent_id)
            missing = order.filled_quantity - sum(
                (f.quantity for f in repo.list_fills(broker_order_id=order.broker_order_id)),
                Decimal(0),
            )
            if missing <= 0:
                continue
            pending[order.symbol] = pending.get(order.symbol, Decimal(0)) + (
                missing if order.side is OrderSide.BUY else -missing
            )
            if order.side is OrderSide.BUY:
                if order.filled_average_price is None:
                    return False
                buy_basis[order.symbol] = order.filled_average_price
        expected, actual = dict(proof.expected_quantities), dict(proof.actual_quantities)
        delta = {
            s: actual.get(s, Decimal(0)) - expected.get(s, Decimal(0))
            for s in expected.keys() | actual.keys()
            if actual.get(s, 0) != expected.get(s, 0)
        }
        # A stable exact owned acknowledgement can precede the POST response/outbox
        # transition. This only explains a retry; it grants no fill or inventory authority.
        if (
            ambiguous - acknowledged
            or not delta
            or delta != {s: q for s, q in pending.items() if q}
        ):
            return False
        certification = latest_certification(session)
        if certification is None:
            return False
        encoded = session.execute(
            select(table("core_reconciliation_proofs").c.payload).where(
                table("core_reconciliation_proofs").c.id == certification[1]
            )
        ).scalar_one_or_none()
        if encoded is None:
            return False
        # Historical basis only; revoked cash remains unusable as present authority.
        previous = ReconciliationProof.model_validate_json(encoded)
        if previous.id != certification[1] or previous.baseline_id != proof.baseline_id:
            return False
        basis = {p.symbol: p.average_entry_price for p in previous.positions}
        basis.update(buy_basis)
        explained = all(p.average_entry_price == basis.get(p.symbol) for p in proof.positions)
        if explained and crossed_sends:
            receipt = dict(
                contract="OWNED_CAPTURE_ACK_RETRY_V2",
                proof_id=proof.id,
                activity_document_id=activity_document_id,
                capture_started=capture_started,
                until=now,
                original_locked_send_ids=tuple(sorted(crossed_sends)),
            )
            put(session, "core_provider_acquisitions", content_id(receipt), receipt)
        return explained


def _inventory_phase(broker: SyntheticBroker | AlpacaCoreBroker, phase: str) -> None:
    if isinstance(broker, AlpacaCoreBroker):
        if phase == "RECOVERY":
            broker.inventory_diagnostic_trace.clear()
        broker.inventory_diagnostic_trace.append((phase, broker.clocks.sample()[1]))


async def synchronize(
    store: CoreStore, broker: SyntheticBroker | AlpacaCoreBroker, *, now: datetime
) -> str:
    require_runtime_broker(store, broker)
    from quantbot.paper_core.outcomes import recover_intents

    capture_started, start = store.clocks.sample()
    funds_capture = paper_balance_policy(store.config)
    open_orders = second_open_orders = None
    activity_document_id = None
    _inventory_phase(broker, "RECOVERY")
    if store.config.clock_profile == "PAPER_TIME_V2":
        async with asyncio.timeout(5):
            recovery = await recover_intents(store, broker)
            if len(recovery.source_refs) == 1:
                activity_document_id = recovery.source_refs[0]
            _inventory_phase(broker, "PAIRED_BROKER_READS")
            account = await broker.get_account()
            positions = await broker.get_positions()
            if funds_capture:
                try:
                    open_orders = tuple(await broker.get_open_orders())
                except (
                    OSError,
                    TimeoutError,
                    BrokerRequestError,
                    BrokerTransportError,
                    BrokerProtocolError,
                ):
                    pass  # Entry funds unavailable; quantity protection still proceeds.
            second_account = await broker.get_account()
            second_positions = await broker.get_positions()
            if funds_capture and open_orders is not None:
                try:
                    second_open_orders = tuple(await broker.get_open_orders())
                except (
                    OSError,
                    TimeoutError,
                    BrokerRequestError,
                    BrokerTransportError,
                    BrokerProtocolError,
                ):
                    pass
        if store.clocks.sample()[1] - start > 5 or (
            not stable_account_reads(account, second_account)
            or not stable_position_reads(positions, second_positions)
            if funds_capture
            else account != second_account or positions != second_positions
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
    proof = None
    _inventory_phase(broker, "WAIT_CERTIFICATION_LOCK")
    try:
        # Network acquisition has finished. Derive and certify the local generation
        # against one locked ledger, without a competing certification gap.
        with store.database.transaction() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            _inventory_phase(broker, "CERTIFICATION_LOCK_ACQUIRED")
            # A send can cross either the provider reads or the subsequent writer
            # wait. In both cases these reads cannot certify the post-send ledger.
            if store.config.clock_profile == "PAPER_TIME_V2":
                now = store.clocks.sample()[0]
            if (
                store.config.clock_profile == "PAPER_TIME_V2"
                and store.clocks.sample()[1] - start > 5
            ):
                raise TimeoutError("INVENTORY_ACQUISITION_UNSTABLE")
            _inventory_phase(broker, "CERTIFICATION_BUILD")
            crossed = (
                _capture_crossing_sends(store, session, capture_started=capture_started, now=now)
                if store.config.clock_profile == "PAPER_TIME_V2"
                and account.account.account_id == store.config.account_id
                else ()
            )
            if crossed:
                reason = "INVENTORY_CAPTURE_CROSSED_OWNED_SEND"
                rejected = dict(
                    contract="INVALIDATED_OWNED_SEND_CAPTURE_V1",
                    account_id=store.config.account_id,
                    release_id=store.runtime_release_id,
                    config_id=store.config_id,
                    capture_started=capture_started,
                    until=now,
                    activity_document_id=activity_document_id,
                    original_locked_send_ids=crossed,
                )
                identity = content_id(rejected)
                put(session, "core_provider_acquisitions", identity, rejected)
                revoked = table("core_cash_authority_revocations")
                full = table("core_inventory_certifications")
                from quantbot.paper_core.work import revoked_proof

                for row in session.execute(
                    select(full).where(full.c.snapshot_id.not_in(select(revoked.c.snapshot_id)))
                ).mappings():
                    session.execute(
                        revoked.insert().values(snapshot_id=row["snapshot_id"], reason=reason)
                    )
                    revoked_proof(session, row["proof_id"], available_at=now)
                report(
                    store,
                    session=session,
                    role="GUARDIAN",
                    state="RETRYABLE_FAILURE",
                    reasons=(reason,),
                    now=now,
                    scope="ACCOUNT",
                    scope_id=store.config.account_id,
                    evidence_ids=(identity,),
                )
                # Persist refusal only. No inventory/funds proof, broker ACK, fill,
                # reservation, intent state or existing quarantine is changed.
                session.commit()
                raise TimeoutError(reason)
            funds = None
            if funds_capture and open_orders is not None and second_open_orders is not None:
                from quantbot.paper_core.authority import commission_in_session

                commission = commission_in_session(store, session)
                funds = PaperFundsEvidence(
                    recorded_at_utc=now,
                    evidence_lane="BROKER_PAPER",
                    release_id=store.config.release_id,
                    endpoint=commission.endpoint,
                    account_id=store.config.account_id,
                    store_identity=store.database.store_identity,
                    config_id=store.config_id,
                    commission_id=commission.id,
                    process_id=store.clocks.process_id,
                    capture_started=capture_started,
                    elapsed_seconds=store.clocks.sample()[1] - start,
                    first_account=account,
                    second_account=second_account,
                    first_positions=positions,
                    second_positions=second_positions,
                    first_open_orders=open_orders,
                    second_open_orders=second_open_orders,
                )
                put(session, "core_provider_acquisitions", funds.id, funds)
            proof = store.proof(
                account.account,
                tuple((p.symbol, p.quantity) for p in positions),
                now=now,
                positions=positions,
                acquisition_id=funds.id
                if funds is not None
                else content_id(
                    {
                        "at": now,
                        "provider": broker.provider,
                        "positions": positions,
                        "account": account.account,
                    }
                ),
                session=session,
            )
            if funds is not None:
                proof = proof.model_copy(update={"funds_authority": funds.contract})
            _inventory_phase(broker, "CERTIFICATION_VALIDATE")
            certify_inventory(store, proof, session=session)
            _inventory_phase(broker, "CERTIFICATION_COMPLETE")
            if (
                store.config.clock_profile == "PAPER_TIME_V2"
                and store.clocks.sample()[1] - start > 5
            ):
                raise TimeoutError("INVENTORY_ACQUISITION_UNSTABLE")
    except StateConflictError:
        if proof is None:
            raise  # Construction refusal retains its original exception and rollback.
        if store.config.clock_profile == "PAPER_TIME_V2":
            retry = _classify_reconciliation_failure(
                store,
                proof,
                capture_started=capture_started,
                now=now,
                activity_document_id=activity_document_id,
            )
            if retry:
                raise TimeoutError(retry) from None
        raise
    if store.parallel_entries and isinstance(broker, AlpacaCoreBroker):
        _inventory_phase(broker, "SESSION_CAPITAL")
        from quantbot.paper_core.portfolio import SessionCapitalEvidence

        broker.entry_source_objections = ()
        if broker.store is None:
            broker.store = store
        try:
            capital_id = await broker.refresh_session_capital(proof)
            if capital_id is None:
                broker.entry_source_objections = ("SESSION_OPENING_UNVERIFIED",)
            else:
                capital = store.read(
                    "core_provider_acquisitions", capital_id, SessionCapitalEvidence
                )
                if not capital.complete:
                    broker.entry_source_objections = ("SESSION_CAPITAL_CONSERVATION_UNVERIFIED",)
                if capital.external_flow_ids:
                    broker.entry_source_objections += ("SESSION_EXTERNAL_CAPITAL_FLOW",)
        except (
            ValueError,
            OSError,
            TimeoutError,
            BrokerRequestError,
            BrokerTransportError,
            BrokerProtocolError,
        ) as error:
            broker.entry_source_objections = (
                "SESSION_CAPITAL_SOURCE_UNAVAILABLE:" + type(error).__name__,
            )
        # No entry cash receipt is invented on failure. Certified quantities still protect exits.
    _inventory_phase(broker, "COMPLETE")
    return proof.id


def _classify_reconciliation_failure(
    store: CoreStore,
    proof: ReconciliationProof,
    *,
    capture_started: datetime,
    now: datetime,
    activity_document_id: str | None,
) -> str | None:
    """Classify and append under one writer lock; stale refusals cannot quarantine."""
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        latest = latest_certification(session)
        quantities, cash, fills = store.ledger(session)
        if (
            latest is not None
            and proof.generation != latest[0] + 1
            or tuple(sorted(quantities.items())) != proof.expected_quantities
            or cash != proof.expected_cash
            or fills != proof.fill_ids
            or store.ledger_state_id(session) != proof.ledger_state_id
            or tuple(
                sorted(o.broker_order_id for o in StorageRepository(session).list_broker_orders())
            )
            != tuple(sorted(proof.order_ids))
        ):
            report(
                store,
                session=session,
                role="GUARDIAN",
                state="RETRYABLE_FAILURE",
                reasons=("RECONCILIATION_AUTHORITY_CHANGED",),
                now=now,
                evidence_ids=(proof.id,),
            )
            return "RECONCILIATION_AUTHORITY_CHANGED"
        mismatched = {
            s
            for s in set(dict(proof.actual_quantities)) | set(dict(proof.expected_quantities))
            if dict(proof.actual_quantities).get(s, 0) != dict(proof.expected_quantities).get(s, 0)
        }
        if mismatched and _pending_owned_activity(
            store,
            proof,
            capture_started=capture_started,
            now=now,
            activity_document_id=activity_document_id,
            session=session,
        ):
            report(
                store,
                session=session,
                role="GUARDIAN",
                state="RETRYABLE_FAILURE",
                reasons=("OWNED_FILL_ACTIVITY_PENDING",),
                now=now,
                scope="ACCOUNT",
                scope_id=store.config.account_id,
                evidence_ids=(proof.id,),
            )
            return "OWNED_FILL_ACTIVITY_PENDING"
        if proof.account.account_id != store.config.account_id or (
            proof.cash_complete and proof.account.cash != proof.expected_cash
        ):
            report(
                store,
                session=session,
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
                    session=session,
                    role="GUARDIAN",
                    state="INTEGRITY_FAILURE",
                    scope="SYMBOL",
                    scope_id=symbol,
                    reasons=("INVENTORY_OWNERSHIP_CONTRADICTION",),
                    now=now,
                    evidence_ids=(proof.id,),
                    protection_blocked=True,
                )
    return None


def acknowledge_handoffs(store: CoreStore, *, now: datetime) -> GuardianReceipt:
    if (
        store.config.evidence_lane == "BROKER_PAPER"
        or getattr(store, "runtime_role", None) is not None
    ) and getattr(store, "runtime_role", None) != "guardian":
        raise ValueError("only independent guardian runtime may acknowledge protection")
    if store.runtime_role is not None or store.config.evidence_lane == "BROKER_PAPER":
        with store.database.transaction() as session:
            links = table("core_intent_links")
            entries = list(
                session.execute(
                    select(links.c.intent_id, links.c.protection_id).where(
                        links.c.role == "ENTRY",
                        links.c.lot_id.not_in(select(table("core_round_trips").c.id)),
                    )
                ).all()
            )
            from quantbot.paper_core.settlement import definitively_unsent_many

            # ERROR alone can mean a lost POST acknowledgement. Retire only the
            # shared durable pre-I/O refusal proof, never a guessed terminal state.
            unsent = definitively_unsent_many(
                StorageRepository(session), [row.intent_id for row in entries]
            )
            active = list(
                dict.fromkeys(row.protection_id for row in entries if not unsent[row.intent_id])
            )
            plans = table("core_protection_plans")
            retained: dict[str, str] = {}
            for offset in range(0, len(active), 500):
                retained.update(
                    (row[0], row[1])
                    for row in session.execute(
                        select(plans.c.id, plans.c.payload).where(
                            plans.c.id.in_(active[offset : offset + 500])
                        )
                    ).all()
                )
            protection_ids = []
            for identity in active:
                payload = retained.get(identity)
                plan = (
                    ProtectionPlan.model_validate_json(payload)
                    if payload is not None
                    else store.read("core_protection_plans", identity, ProtectionPlan)
                )
                if plan.id != identity:
                    raise ValueError("PROTECTION_PLAN_ID_MISMATCH")
                protection_ids.append(plan.id)
    else:
        protection_ids = [content_id(p) for p in store.documents("core_protection_plans")]
    with store.database.transaction() as session:
        receipts, current = table("core_guardian_receipts"), table("core_guardian_current")
        payload = session.execute(
            select(receipts.c.payload).join(current, current.c.receipt_id == receipts.c.id)
        ).scalar_one_or_none()
        previous = GuardianReceipt.model_validate_json(payload) if payload else None
        # Immutable receipts retain their original identities and historical joins.
        # Renew halfway through validity, and publish changed coverage immediately.
        if (
            previous is not None
            and previous.evidence_lane == store.config.evidence_lane
            and previous.release_id == store.config.release_id
            and previous.owner == store.config.owner
            and previous.generation == store.config.generation
            and set(previous.protection_ids) == set(protection_ids)
            and previous.recorded_at_utc <= now
            and now + timedelta(seconds=60) <= previous.valid_until
        ):
            return previous
    receipt = GuardianReceipt(
        recorded_at_utc=now,
        evidence_lane=store.config.evidence_lane,
        release_id=store.config.release_id,
        owner=store.config.owner,
        generation=store.config.generation,
        valid_until=now + timedelta(seconds=120),
        protection_ids=tuple(protection_ids),
        source_refs=tuple(protection_ids),
    )
    store.record("core_guardian_receipts", receipt)
    return receipt


# Operational submission headroom, never a rewrite of the immutable holding deadline.
DEADLINE_SUBMISSION_LEAD_SECONDS = 30


def _owned_exit_candidates(
    store: CoreStore, now: datetime
) -> tuple[tuple[ProtectionPlan, str, Decimal], ...]:
    """Scheduling hints only; actual quantities/reservations are rechecked before send."""
    candidates = []
    with store.database.transaction() as session:
        links = table("core_intent_links")
        rows = list(
            session.execute(
                select(links).where(
                    links.c.role == "ENTRY",
                    links.c.lot_id.not_in(select(table("core_round_trips").c.id)),
                )
            ).mappings()
        )
        repo = StorageRepository(session)
        intents = repo.get_order_intents([row["intent_id"] for row in rows])
        orders = iter(
            repo.get_broker_orders_by_client_order_ids(
                [intent.client_order_id for intent in intents if intent is not None]
            )
        )
        for row, intent in zip(rows, intents, strict=True):
            if intent is None:
                continue
            order = next(orders)
            fills = repo.list_fills(broker_order_id=order.broker_order_id) if order else []
            if not fills:
                continue
            from quantbot.paper_core.outcomes import terminal_evidence
            from quantbot.paper_core.send_guard import definitively_unsent

            remaining = sum((f.quantity for f in fills), Decimal(0))
            needs_recovery = False
            for exit_link in session.execute(
                select(links).where(links.c.lot_id == row["lot_id"], links.c.role == "EXIT")
            ).mappings():
                previous = repo.get_order_intent(exit_link["intent_id"])
                if previous is None or definitively_unsent(repo, previous.intent_id):
                    continue
                previous_order = repo.get_broker_order_by_client_order_id(previous.client_order_id)
                if previous_order is not None:
                    remaining -= sum(
                        (
                            f.quantity
                            for f in repo.list_fills(broker_order_id=previous_order.broker_order_id)
                        ),
                        Decimal(0),
                    )
                if (
                    previous_order is None
                    or terminal_evidence(session, previous_order, cutoff=now) is None
                ):
                    needs_recovery = True
            if needs_recovery or remaining <= 0:
                # Pending/canceling exits need canonical recovery. Skipping it forever
                # would strand a partial fill after its first prioritized send pass.
                continue
            plan = store.read("core_protection_plans", row["protection_id"], ProtectionPlan)
            average = sum((f.price * f.quantity for f in fills), Decimal(0)) / sum(
                (f.quantity for f in fills), Decimal(0)
            )
            candidates.append((plan, intent.symbol, average))
    return tuple(candidates)


def owned_deadline_wait(store: CoreStore) -> float | None:
    """Seconds until submission headroom; wall fallback can only schedule a wake/read."""
    if not store.parallel_entries:
        return None
    now = store.clocks.sample()[0]
    if store.runtime_time_evidence_id is not None:
        timing = store.read("core_time_evidence", store.runtime_time_evidence_id, TimeEvidence)
        if not evidence_objections(timing, store.clocks):
            now = provider_interval(timing, store.clocks)[0]
    waits = [
        (plan.deadline - now).total_seconds() - DEADLINE_SUBMISSION_LEAD_SECONDS
        for plan, _, _ in _owned_exit_candidates(store, now)
    ]
    return min(waits) if waits else None


def _known_exit_due(
    store: CoreStore,
    broker: SyntheticBroker | AlpacaCoreBroker,
    timing: TimeEvidence | None,
    now: datetime,
) -> bool:
    """Prioritize owned deadline headroom/stops; this is not exit/send authority."""
    for plan, symbol, average in _owned_exit_candidates(store, now):
        if timing is not None and not evidence_objections(timing, store.clocks):
            if provider_interval(timing, store.clocks)[1] >= plan.deadline - timedelta(
                seconds=DEADLINE_SUBMISSION_LEAD_SECONDS
            ):
                return True
        elif plan.clock_profile != "PAPER_TIME_V2" and now >= plan.deadline:
            return True
        mark = broker.mark(symbol)
        if (
            mark
            and 0 <= (now - mark[1]).total_seconds() <= 120
            and mark[0] <= average * Decimal(".99")
        ):
            return True
    return False


async def guardian_cycle(
    store: CoreStore, broker: SyntheticBroker | AlpacaCoreBroker, *, now: datetime
) -> CycleReceipt:
    require_runtime_broker(store, broker)
    acknowledge_handoffs(store, now=now)
    time_evidence = None
    reasons: tuple[str, ...] = ()
    proof_id = None
    if store.config.clock_profile == "PAPER_TIME_V2":
        time_evidence = await acquire_time_evidence(broker, store.clocks, context=store.config)
        store.record("core_time_evidence", time_evidence)
        store.runtime_time_evidence_id = time_evidence.id
        if evidence_objections(time_evidence, store.clocks):
            reasons = time_evidence.objections or evidence_objections(time_evidence, store.clocks)
            time_evidence = None
    prioritize_owned = store.parallel_entries and _known_exit_due(
        store, broker, time_evidence, broker.now
    )
    if not prioritize_owned:
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
    if store.parallel_entries:
        reasons += getattr(broker, "entry_source_objections", ())
    exits = []
    with store.database.transaction() as session:
        links = table("core_intent_links")
        active_entries = list(
            session.execute(
                select(links).where(
                    links.c.role == "ENTRY",
                    links.c.lot_id.not_in(select(table("core_round_trips").c.id)),
                )
            ).mappings()
        )
        plans = table("core_protection_plans")
        plan_ids = [entry["protection_id"] for entry in active_entries]
        retained_plans: dict[str, str] = {}
        for offset in range(0, len(plan_ids), 500):
            retained_plans.update(
                (row[0], row[1])
                for row in session.execute(
                    select(plans.c.id, plans.c.payload).where(
                        plans.c.id.in_(plan_ids[offset : offset + 500])
                    )
                ).all()
            )
    for entry in active_entries:
        if store.config.clock_profile == "PAPER_TIME_V2":
            now = broker.now
        # Only immutable payloads cross an await. Validate lazily at the original
        # per-entry point; mutable intent/order/fill ownership remains freshly read.
        payload = retained_plans.get(entry["protection_id"])
        plan = (
            ProtectionPlan.model_validate_json(payload)
            if payload is not None
            else store.read("core_protection_plans", entry["protection_id"], ProtectionPlan)
        )
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
            blocked = [
                h
                for h in active_health(session)
                if h.state == "INTEGRITY_FAILURE"
                and h.protection_blocked
                and (
                    h.scope == "ACCOUNT"
                    and h.scope_id == store.config.account_id
                    or h.scope == "SYMBOL"
                    and h.scope_id == intent.symbol
                    or h.scope == "LOT"
                    and h.scope_id == entry["lot_id"]
                )
            ]
            if blocked:
                reasons = (*reasons, *("OWNED_PROTECTION_QUARANTINED:" + h.id for h in blocked))
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
        send_ahead = (
            store.parallel_entries
            and plan.clock_profile == "PAPER_TIME_V2"
            and time_evidence is not None
            and not evidence_objections(time_evidence, store.clocks)
            and provider_interval(time_evidence, store.clocks)[0]
            >= plan.deadline - timedelta(seconds=DEADLINE_SUBMISSION_LEAD_SECONDS)
        )
        if not due and not stop and not send_ahead:
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
                from quantbot.paper_core.send_guard import definitively_unsent

                if definitively_unsent(repo, previous.intent_id):
                    continue
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
                    time_in_force=(
                        intent.time_in_force if store.config.asset_capabilities else TimeInForce.DAY
                    ),
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
                decision = store.read("core_decisions", entry["decision_id"], Decision)
                repo.record_order_event(
                    "core-exit-trigger-" + content_id({"intent": exit_intent.intent_id}),
                    event_type="CORE_OWNED_EXIT_TRIGGERED",
                    occurred_at=now,
                    intent_id=exit_intent.intent_id,
                    detail=dict(
                        lot_id=entry["lot_id"],
                        protection_id=plan.id,
                        trigger="STOP"
                        if stop
                        else ("SESSION_CLOSE" if due else "SESSION_CLOSE_PREEMPTIVE")
                        if plan.deadline
                        == decision.session_close - timedelta(seconds=plan.exit_buffer_seconds)
                        else ("DEADLINE" if due else "DEADLINE_PREEMPTIVE"),
                        deadline=plan.deadline.isoformat(),
                        stop_due=stop,
                        deadline_due=due,
                        submission_lead_seconds=DEADLINE_SUBMISSION_LEAD_SECONDS
                        if send_ahead and not due and not stop
                        else 0,
                        original_stop=str(plan.stop_price),
                        stop_basis=str(average * Decimal(".99")),
                        mark_price=str(mark[0]) if mark else None,
                        mark_at=mark[1].isoformat() if mark else None,
                        time_evidence_id=time_evidence.id if time_evidence else None,
                    ),
                )
            if store.parallel_entries and exit_intent.state is IntentState.RISK_APPROVED:
                # Owned quantity/competing exits are reserved above. The retained
                # coordinator still runs every fresh final gate and locked send CAS.
                # Avoid duplicating the identical account/position/open-order reads.
                exit_intent = repo.transition_order_intent(
                    exit_intent.intent_id,
                    IntentState.ORDER_CREATED,
                    expected_current=IntentState.RISK_APPROVED,
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
        if not store.parallel_entries:
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
    if (exits or prioritize_owned) and store.parallel_entries:
        try:
            from quantbot.paper_core.account_control import request_priority

            # Each exit used fresh broker quantity/account checks and locked lot reservations.
            # Reconcile the completed group once, rather than N full-history acquisitions.
            with request_priority("OWNED_EXIT_RECONCILIATION"):
                proof_id = await synchronize(store, broker, now=broker.now)
        except (
            TimeoutError,
            OSError,
            StateConflictError,
            BrokerRequestError,
            BrokerTransportError,
        ):
            reasons += ("POST_EXIT_RECONCILIATION_WAIT",)
    from quantbot.paper_core.outcomes import process_outcomes

    if store.config.clock_profile == "PAPER_TIME_V2":
        now = broker.now
    process_outcomes(store, cutoff=now)
    if (
        proof_id is not None
        and store.runtime_time_evidence_id is not None
        and not set(reasons) - {"SESSION_OPENING_UNVERIFIED"}
    ):
        from quantbot.paper_core.health import recover_inventory_health

        recover_inventory_health(
            store,
            store.read("core_reconciliation_proofs", proof_id, ReconciliationProof),
            store.read("core_time_evidence", store.runtime_time_evidence_id, TimeEvidence),
            now=now,
        )
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
