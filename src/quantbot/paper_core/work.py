"""Transaction-coupled bounded outcome work; notifications never delete history."""

import json
from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from quantbot.paper_core.contracts import CashActivity, GuardianReceipt
from quantbot.paper_core.store import table

DOCUMENT_REFERENCES = {
    "core_broker_event_sources": "client_order_id",
    "core_outcome_revisions": "lot_id",
    "core_cost_items": "fill_id",
    "core_reconciliation_proofs": "account",
    "core_round_trips": "reconciliation_id",
}


def index_document(session: Session, name: str, identity: str, data: dict[str, Any]) -> None:
    if name not in DOCUMENT_REFERENCES:
        return
    reference = data[DOCUMENT_REFERENCES[name]]
    if name == "core_reconciliation_proofs":
        reference = reference["account_id"]
    index = table("core_document_index")
    values = dict(reference=reference, available_at=data["recorded_at_utc"])
    existing = session.execute(
        select(index.c.id).where(index.c.kind == name, index.c.id == identity)
    ).first()
    if existing:
        session.execute(
            update(index).where(index.c.kind == name, index.c.id == identity).values(**values)
        )
    else:
        session.execute(index.insert().values(kind=name, id=identity, **values))
        if name == "core_reconciliation_proofs" and data["fill_ids"]:
            session.execute(
                table("core_proof_fill_memberships").insert(),
                [{"proof_id": identity, "fill_id": f} for f in sorted(set(data["fill_ids"]))],
            )
        if name == "core_broker_event_sources" and data.get("cash_activity"):
            activity = CashActivity.model_validate(data["cash_activity"])
            session.execute(
                index.insert().values(
                    kind="core_cash_activity_sources",
                    id=identity,
                    reference=activity.id,
                    available_at=data["recorded_at_utc"],
                )
            )


def indexed(session: Session, name: str, *, reference: str | None = None) -> Select[Any]:
    documents, index = table(name), table("core_document_index")
    query = select(documents).join(index, documents.c.id == index.c.id).where(index.c.kind == name)
    return query.where(index.c.reference == reference) if reference is not None else query


def dirty(session: Session, lots: Iterable[str]) -> None:
    work = table("core_outcome_work")
    for lot in set(lots):
        version = session.execute(
            select(work.c.version).where(work.c.lot_id == lot)
        ).scalar_one_or_none()
        if version is None:
            session.execute(
                work.insert().values(lot_id=lot, version=1, processed_version=0, attempted_at="")
            )
        else:
            session.execute(update(work).where(work.c.lot_id == lot).values(version=version + 1))


def pending(session: Session) -> list[str]:
    links, completed = table("core_intent_links"), table("core_round_trips")
    return list(
        session.execute(
            select(links.c.lot_id).where(
                links.c.role == "ENTRY", links.c.lot_id.not_in(select(completed.c.id))
            )
        ).scalars()
    )


def notify_document(session: Session, name: str, identity: str, payload: str) -> None:
    if name in DOCUMENT_REFERENCES:
        index_document(session, name, identity, json.loads(payload))
    if name not in {
        "core_cost_items",
        "core_broker_event_sources",
        "core_guardian_receipts",
        "core_reconciliation_proofs",
    }:
        return
    data = json.loads(payload)
    if name == "core_guardian_receipts":
        memberships, current = table("core_guardian_memberships"), table("core_guardian_current")
        for plan in data["protection_ids"]:
            session.execute(memberships.insert().values(plan_id=plan, receipt_id=identity))
        previous = session.execute(select(current.c.receipt_id)).scalar_one_or_none()
        if previous is None:
            session.execute(current.insert().values(id="current", receipt_id=identity))
        else:
            old = session.execute(
                select(table(name).c.payload).where(table(name).c.id == previous)
            ).scalar_one()
            if data["recorded_at_utc"] >= json.loads(old)["recorded_at_utc"]:
                session.execute(update(current).values(receipt_id=identity))
        dirty(session, pending(session))
    elif name == "core_reconciliation_proofs":
        dirty(session, pending(session))
    else:
        links, intents = table("core_intent_links"), table("order_intents")
        query = select(links.c.lot_id).join(intents, links.c.intent_id == intents.c.intent_id)
        if name == "core_cost_items":
            fills, orders = table("fills"), table("broker_orders")
            query = (
                query.join(orders, orders.c.client_order_id == intents.c.client_order_id)
                .join(fills, fills.c.broker_order_id == orders.c.broker_order_id)
                .where(fills.c.fill_id == data["fill_id"])
            )
        else:
            query = query.where(intents.c.client_order_id == data["client_order_id"])
        dirty(session, session.execute(query).scalars())


def current_receipts(session: Session, *, plan_id: str | None = None) -> list[GuardianReceipt]:
    """Current execution uses one exact pointer; historical joins use memberships."""
    docs, pointer = table("core_guardian_receipts"), table("core_guardian_current")
    heartbeat_docs, roles = table("core_role_heartbeats"), table("core_role_current")
    heartbeat_payload = session.execute(
        select(heartbeat_docs.c.payload)
        .join(roles, roles.c.heartbeat_id == heartbeat_docs.c.id)
        .where(roles.c.role == "guardian")
    ).scalar_one_or_none()
    if heartbeat_payload is not None:
        receipt_id = json.loads(heartbeat_payload).get("guardian_receipt_id")
        payload = session.execute(
            select(docs.c.payload).where(docs.c.id == receipt_id)
        ).scalar_one_or_none()
        receipt = GuardianReceipt.model_validate_json(payload) if payload else None
        return (
            [receipt] if receipt and (plan_id is None or plan_id in receipt.protection_ids) else []
        )
    payload = session.execute(
        select(docs.c.payload).join(pointer, pointer.c.receipt_id == docs.c.id)
    ).scalar_one_or_none()
    if payload is None:
        # Cold raw-fixture/recovery support. Never used after normal receipt publication.
        payload = session.execute(
            select(docs.c.payload)
            .order_by(
                func.json_extract(docs.c.payload, "$.recorded_at_utc").desc(), docs.c.id.desc()
            )
            .limit(1)
        ).scalar_one_or_none()
    receipt = GuardianReceipt.model_validate_json(payload) if payload else None
    return [receipt] if receipt and (plan_id is None or plan_id in receipt.protection_ids) else []


def historical_receipts(session: Session, plan_id: str) -> list[GuardianReceipt]:
    docs, membership = table("core_guardian_receipts"), table("core_guardian_memberships")
    rows = (
        session.execute(
            select(docs.c.payload)
            .join(membership, membership.c.receipt_id == docs.c.id)
            .where(membership.c.plan_id == plan_id)
        )
        .scalars()
        .all()
    )
    if not rows:
        rows = (
            session.execute(select(docs.c.payload).where(docs.c.payload.contains(plan_id)))
            .scalars()
            .all()
        )
    return [GuardianReceipt.model_validate_json(p) for p in rows]


def revoked_proof(session: Session, proof_id: str) -> None:
    trips = table("core_round_trips")
    query = indexed(session, "core_round_trips", reference=proof_id).with_only_columns(trips.c.id)
    dirty(session, session.execute(query).scalars())
    dirty(session, pending(session))
