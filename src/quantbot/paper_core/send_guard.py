"""Durable entry lifetime consumed at the coordinator's actual send boundary."""

from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import select, text

from quantbot.domain import IntentState, OrderIntent
from quantbot.paper_core.contracts import (
    Decision,
    GuardianReceipt,
    ProtectionPlan,
    ReconciliationProof,
)
from quantbot.paper_core.store import CoreStore, latest_certification, table
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
            if r.owner == plan.owner
            and r.generation == plan.generation
            and plan.id in r.protection_ids
        ]
        receipt = max(receipts, key=lambda r: (r.recorded_at_utc, r.id), default=None)
        # Sample after durable reads and writer-lock acquisition, never the caller's decision time.
        now = clock()
        reasons = []
        if any(
            i.kind == "CORE_EVIDENCE_CONFLICT" for i in repo.list_incidents(unresolved_only=True)
        ):
            reasons.append("CORE_EVIDENCE_QUARANTINED")
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
