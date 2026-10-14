"""Durable entry lifetime consumed at the coordinator's actual send boundary."""

from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from quantbot.domain import IntentState, OrderIntent
from quantbot.paper_core.contracts import (
    Decision,
    GuardianReceipt,
    ProtectionPlan,
    ReconciliationProof,
)
from quantbot.paper_core.health import entry_health_objections
from quantbot.paper_core.store import CoreStore, latest_certification, table
from quantbot.paper_core.time_evidence import TimeEvidence, evidence_objections, provider_interval
from quantbot.research.manifest import content_id
from quantbot.risk import OrderPurpose
from quantbot.storage import StorageRepository

REFUSAL = "CORE_ENTRY_SEND_REFUSED"


def definitively_unsent(repository: StorageRepository, intent_id: str) -> bool:
    intent = repository.get_order_intent(intent_id)
    if intent is None or intent.state is not IntentState.ERROR:
        return False
    if repository.get_broker_order_by_client_order_id(intent.client_order_id) is not None:
        return False
    # ORDER_CREATED cannot follow SUBMITTING under the retained lifecycle. The guard
    # latches refusal before leaving that state; later guard invocations never POST it.
    return any(
        event.event_type == REFUSAL
        and event.detail.get("definitively_unsent") is True
        and event.detail.get("state_at_refusal") == IntentState.ORDER_CREATED.value
        and event.detail.get("guard_contract") == "core-final-send-v1"
        for event in repository.list_order_events(intent_id=intent_id)
    )


def final_send_objections(
    store: CoreStore, intent: OrderIntent, purpose: OrderPurpose, *, clock: Callable[[], datetime]
) -> tuple[str, ...]:
    if purpose is not OrderPurpose.ENTRY:
        return ()
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        return final_send_in_session(store, session, intent, purpose, clock=clock)


def final_send_in_session(
    store: CoreStore,
    session: Session,
    intent: OrderIntent,
    purpose: OrderPurpose,
    *,
    clock: Callable[[], datetime],
) -> tuple[str, ...]:
    """Validate authority using the coordinator's locked send-state transaction."""
    if purpose is not OrderPurpose.ENTRY:
        return ()
    repo = StorageRepository(session)
    stored = repo.get_order_intent(intent.intent_id)
    links = table("core_intent_links")
    link = (
        session.execute(
            select(links).where(links.c.intent_id == intent.intent_id, links.c.role == "ENTRY")
        )
        .mappings()
        .one_or_none()
    )
    if stored is None or link is None:
        return ("CORE_ENTRY_HANDOFF_MISSING",)
    # A crash after refusal evidence but before coordinator ERROR cannot reopen authority
    # when fixture time reverses. Preserve the old refusal rather than record another claim.
    if any(e.event_type == REFUSAL for e in repo.list_order_events(intent_id=intent.intent_id)):
        return ("CORE_ENTRY_ALREADY_REFUSED",)
    decision = Decision.model_validate_json(
        session.execute(
            select(table("core_decisions").c.payload).where(
                table("core_decisions").c.id == link["decision_id"]
            )
        ).scalar_one()
    )
    plan = ProtectionPlan.model_validate_json(
        session.execute(
            select(table("core_protection_plans").c.payload).where(
                table("core_protection_plans").c.id == link["protection_id"]
            )
        ).scalar_one()
    )
    receipts = [
        GuardianReceipt.model_validate_json(p)
        for p in session.execute(select(table("core_guardian_receipts").c.payload)).scalars()
    ]
    receipts = [
        r
        for r in receipts
        if r.owner == plan.owner and r.generation == plan.generation and plan.id in r.protection_ids
    ]
    receipt = max(receipts, key=lambda r: (r.recorded_at_utc, r.id), default=None)
    # Sample after durable reads and writer-lock acquisition, never the caller's decision time.
    now = store.clocks.sample()[0] if store.config.clock_profile == "PAPER_TIME_V2" else clock()
    reasons = []
    if repo.get_kill_switch_state().engaged:
        reasons.append("CORE_MANUAL_ENTRY_PAUSE")
    reasons.extend(entry_health_objections(store, session, decision.symbol))
    if store.config.clock_profile == "PAPER_TIME_V2":
        payload = session.execute(
            select(table("core_time_evidence").c.payload).where(
                table("core_time_evidence").c.id == decision.time_evidence_id
            )
        ).scalar_one_or_none()
        if payload is None or plan.clock_profile != store.config.clock_profile:
            reasons.append("TIME_EVIDENCE_MISSING")
        else:
            evidence = TimeEvidence.model_validate_json(payload)
            reasons.extend(evidence_objections(evidence, store.clocks))
            earliest, latest_time = provider_interval(evidence, store.clocks)
            if latest_time >= decision.valid_until:
                reasons.append("TIME_OBSERVATION_BOUNDARY")
            if latest_time >= plan.deadline:
                reasons.append("CORE_PROTECTION_DEADLINE_DUE")
            if latest_time >= decision.session_close - timedelta(seconds=120):
                reasons.append("TIME_SESSION_BOUNDARY")
            if evidence.samples and earliest < evidence.samples[-1].provider.session_open:
                reasons.append("TIME_SESSION_BOUNDARY")
    latest = latest_certification(session)
    proof = (
        None
        if latest is None
        else ReconciliationProof.model_validate_json(
            session.execute(
                select(table("core_reconciliation_proofs").c.payload).where(
                    table("core_reconciliation_proofs").c.id == latest[1]
                )
            ).scalar_one()
        )
    )
    if (
        proof is None
        or not proof.cash_complete
        or proof.expected_cash is None
        or proof.account.cash != proof.expected_cash
        or not store.cash_authority_current(session, proof)
        or decision.inventory_id != proof.id
        or session.execute(
            select(table("core_cash_authority_revocations").c.snapshot_id).where(
                table("core_cash_authority_revocations").c.snapshot_id
                == "core-certified-" + proof.id
            )
        ).first()
    ):
        reasons.append("CORE_CASH_AUTHORITY_UNAVAILABLE")
    if (
        stored.state is not IntentState.ORDER_CREATED
        or intent.state is not IntentState.ORDER_CREATED
        or stored != intent
        or decision.id != link["decision_id"]
        or plan.id != link["protection_id"]
        or plan.decision_id != decision.id
        or decision.config_id != store.config_id
        or decision.observation_id != link["observation_id"]
        or plan.owner != store.config.owner
        or plan.generation != store.config.generation
    ):
        reasons.append("CORE_ENTRY_AUTHORITY_MISMATCH")
    if not decision.recorded_at_utc <= now < decision.valid_until:
        reasons.append("CORE_ENTRY_DECISION_EXPIRED")
    if not plan.recorded_at_utc <= now < plan.deadline:
        reasons.append("CORE_PROTECTION_DEADLINE_DUE")
    if not now < decision.session_close - timedelta(seconds=plan.exit_buffer_seconds):
        reasons.append("CORE_SESSION_EXIT_BUFFER_DUE")
    if receipt is None or not receipt.recorded_at_utc <= now < receipt.valid_until:
        reasons.append("CORE_GUARDIAN_RECEIPT_EXPIRED")
    if reasons:
        definitive = (
            stored.state is IntentState.ORDER_CREATED
            and repo.get_broker_order_by_client_order_id(intent.client_order_id) is None
        )
        repo.record_order_event(
            "core-send-refused-" + content_id({"intent": intent.intent_id}),
            event_type=REFUSAL,
            occurred_at=now,
            intent_id=intent.intent_id,
            detail={
                "guard_contract": "core-final-send-v1",
                "reasons": tuple(reasons),
                "definitively_unsent": definitive,
                "state_at_refusal": stored.state.value,
                "decision_id": decision.id,
                "protection_id": plan.id,
                "guardian_receipt_id": None if receipt is None else receipt.id,
            },
        )
    return tuple(reasons)


def retire_stale_entries(store: CoreStore, *, clock: Callable[[], datetime]) -> tuple[str, ...]:
    """A durable pre-send state proves no POST was possible; absence/404 does not."""
    retired = []
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        repo = StorageRepository(session)
        links = table("core_intent_links")
        for link in session.execute(select(links).where(links.c.role == "ENTRY")).mappings():
            intent = repo.get_order_intent(link["intent_id"])
            if intent is None or intent.state not in {
                IntentState.RISK_APPROVED,
                IntentState.ORDER_CREATED,
            }:
                continue
            if repo.get_broker_order_by_client_order_id(intent.client_order_id) is not None:
                continue
            decision = Decision.model_validate_json(
                session.execute(
                    select(table("core_decisions").c.payload).where(
                        table("core_decisions").c.id == link["decision_id"]
                    )
                ).scalar_one()
            )
            plan = ProtectionPlan.model_validate_json(
                session.execute(
                    select(table("core_protection_plans").c.payload).where(
                        table("core_protection_plans").c.id == link["protection_id"]
                    )
                ).scalar_one()
            )
            if (
                decision.id != link["decision_id"]
                or plan.id != link["protection_id"]
                or plan.decision_id != decision.id
                or decision.config_id != store.config_id
                or plan.owner != store.config.owner
                or plan.generation != store.config.generation
            ):
                continue
            now = clock()
            old_refusal = any(
                e.event_type == REFUSAL
                and e.detail.get("definitively_unsent") is True
                and e.detail.get("state_at_refusal") == IntentState.ORDER_CREATED.value
                and e.detail.get("guard_contract") == "core-final-send-v1"
                for e in repo.list_order_events(intent_id=intent.intent_id)
            )
            if not old_refusal and not (
                now >= decision.valid_until
                or now >= plan.deadline
                or now >= decision.session_close - timedelta(seconds=plan.exit_buffer_seconds)
            ):
                continue
            if intent.state is IntentState.RISK_APPROVED:
                intent = repo.transition_order_intent(
                    intent.intent_id,
                    IntentState.ORDER_CREATED,
                    expected_current=IntentState.RISK_APPROVED,
                )
            objections = final_send_in_session(
                store, session, intent, OrderPurpose.ENTRY, clock=clock
            )
            if objections:
                repo.transition_order_intent(
                    intent.intent_id, IntentState.ERROR, expected_current=IntentState.ORDER_CREATED
                )
                retired.append(intent.intent_id)
    return tuple(retired)
