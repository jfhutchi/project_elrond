"""Transaction-coupled bounded outcome work; notifications never delete history."""

import json
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, func, select, text, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from quantbot.paper_core.contracts import CashActivity, GuardianReceipt
from quantbot.paper_core.store import table
from quantbot.research.manifest import content_id
from quantbot.storage.database import decode_utc, encode_utc

NOTIFICATIONS = "core_outcome_notifications"

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
            # A polling proof stays independent and immutable. Only its lookup
            # projection shares an exact fill set; it grants no certification.
            fills = sorted(set(data["fill_ids"]))
            set_id = content_id(fills)
            anchor = session.execute(
                select(index.c.reference).where(
                    index.c.kind == "core_fill_set", index.c.id == set_id
                )
            ).scalar_one_or_none()
            if anchor is None:
                anchor = identity
                session.execute(
                    table("core_proof_fill_memberships").insert(),
                    [{"proof_id": identity, "fill_id": fill} for fill in fills],
                )
                session.execute(
                    index.insert().values(
                        kind="core_fill_set",
                        id=set_id,
                        reference=anchor,
                        available_at=data["recorded_at_utc"],
                    )
                )
            session.execute(
                index.insert().values(
                    kind="core_proof_fill_set",
                    id=identity,
                    reference=anchor,
                    available_at=data["recorded_at_utc"],
                )
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


def dirty(
    session: Session,
    lots: Iterable[str],
    *,
    available_at: datetime | None = None,
    _initial_claim: bool = False,
) -> None:
    work, index = table("core_outcome_work"), table("core_document_index")
    identities = sorted(set(lots))
    if not identities:
        return
    connection = session.connection()
    if connection.dialect.name == "sqlite" and not getattr(
        connection.connection.driver_connection, "in_transaction", False
    ):
        # SQLAlchemy autobegin alone does not establish a SQLite SELECT snapshot.
        # Preserve the caller's real BEGIN/BEGIN IMMEDIATE and never cache across calls.
        session.execute(text("BEGIN"))
    previous_rows: dict[str, RowMapping] = {}
    watermarks: dict[str, RowMapping] = {}
    for offset in range(0, len(identities), 500):
        chunk = identities[offset : offset + 500]
        previous_rows.update(
            (row["lot_id"], row)
            for row in session.execute(select(work).where(work.c.lot_id.in_(chunk))).mappings()
        )
        watermarks.update(
            (row["id"], row)
            for row in session.execute(
                select(index).where(index.c.kind == NOTIFICATIONS, index.c.id.in_(chunk))
            ).mappings()
        )
    new_indexes: list[dict[str, Any]] = []
    changed_indexes: list[dict[str, Any]] = []
    new_work: list[dict[str, Any]] = []
    changed_work: list[dict[str, Any]] = []
    for lot in identities:
        previous = previous_rows.get(lot)
        watermark = watermarks.get(lot)
        version = 1 if previous is None else previous["version"] + 1
        known = available_at is not None and (
            previous is None
            and _initial_claim
            or previous is not None
            and watermark is not None
            and watermark["reference"] == str(previous["version"])
        )
        times = [available_at] if available_at is not None else []
        if watermark is not None and watermark["available_at"]:
            times.append(decode_utc(watermark["available_at"]))
        values = dict(
            reference=str(version) if known else "unknown:" + str(version),
            available_at=encode_utc(max(times)) if times else "",
        )
        if watermark is None:
            new_indexes.append(dict(kind=NOTIFICATIONS, id=lot, **values))
        else:
            changed_indexes.append(
                dict(_lot=lot, _reference=values["reference"], _available_at=values["available_at"])
            )
        if previous is None:
            new_work.append(dict(lot_id=lot, version=1, processed_version=0, attempted_at=""))
        else:
            changed_work.append(dict(_lot=lot, _version=version))
    if new_indexes:
        session.execute(index.insert(), new_indexes)
    if changed_indexes:
        session.execute(
            update(index)
            .where(index.c.kind == NOTIFICATIONS, index.c.id == bindparam("_lot"))
            .values(reference=bindparam("_reference"), available_at=bindparam("_available_at")),
            changed_indexes,
        )
    if new_work:
        session.execute(work.insert(), new_work)
    if changed_work:
        session.execute(
            update(work)
            .where(work.c.lot_id == bindparam("_lot"))
            .values(version=bindparam("_version")),
            changed_work,
        )


def covered_version(
    session: Session, lot: str, version: int, cutoff: datetime, account: str
) -> bool:
    """A historical poll cannot acknowledge later evidence, even before its lock."""
    index = table("core_document_index")
    watermark = (
        session.execute(select(index).where(index.c.kind == NOTIFICATIONS, index.c.id == lot))
        .mappings()
        .one_or_none()
    )
    if watermark is None or watermark["reference"] != str(version):
        # Cold v36 compatibility only: no repeated payload/history reconstruction.
        links, intents, orders, fills = (
            table(n) for n in ("core_intent_links", "order_intents", "broker_orders", "fills")
        )
        clients = (
            select(intents.c.client_order_id)
            .join(links, links.c.intent_id == intents.c.intent_id)
            .where(links.c.lot_id == lot)
        )
        owned_fills = (
            select(fills.c.fill_id)
            .join(orders, orders.c.broker_order_id == fills.c.broker_order_id)
            .where(orders.c.client_order_id.in_(clients))
        )
        times = []
        for kind, refs in (
            ("core_broker_event_sources", clients),
            ("core_cost_items", owned_fills),
        ):
            stamp = session.execute(
                select(index.c.available_at)
                .where(index.c.kind == kind, index.c.reference.in_(refs))
                .order_by(index.c.available_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if stamp:
                times.append(decode_utc(stamp).replace(microsecond=0) + timedelta(seconds=1))
        stamp = session.execute(
            select(index.c.available_at)
            .where(index.c.kind == "core_reconciliation_proofs", index.c.reference == account)
            .order_by(index.c.available_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if stamp:
            times.append(decode_utc(stamp).replace(microsecond=0) + timedelta(seconds=1))
        receipts, pointer = table("core_guardian_receipts"), table("core_guardian_current")
        payload = session.execute(
            select(receipts.c.payload).join(pointer, pointer.c.receipt_id == receipts.c.id)
        ).scalar_one_or_none()
        if payload:
            # The publication pointer is maximum serialized UTC timestamp, not insertion order.
            times.append(
                decode_utc(json.loads(payload)["recorded_at_utc"]).replace(microsecond=0)
                + timedelta(seconds=1)
            )
        decisions = table("core_decisions")
        stamps = session.execute(
            select(func.json_extract(decisions.c.payload, "$.recorded_at_utc")).where(
                decisions.c.id == lot
            )
        ).scalars()
        times.extend(decode_utc(stamp) for stamp in stamps if stamp)
        if watermark is not None and watermark["available_at"]:
            times.append(decode_utc(watermark["available_at"]))
        if not times:
            return False  # Missing provenance never silently grants acknowledgement.
        values = dict(reference=str(version), available_at=encode_utc(max(times)))
        if watermark is None:
            session.execute(index.insert().values(kind=NOTIFICATIONS, id=lot, **values))
        else:
            session.execute(
                update(index)
                .where(index.c.kind == NOTIFICATIONS, index.c.id == lot)
                .values(**values)
            )
        return decode_utc(values["available_at"]) <= cutoff
    return bool(
        watermark and watermark["available_at"] and decode_utc(watermark["available_at"]) <= cutoff
    )


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
        plans = data["protection_ids"]
        for offset in range(0, len(plans), 500):
            session.execute(
                memberships.insert(),
                [
                    {"plan_id": plan, "receipt_id": identity}
                    for plan in plans[offset : offset + 500]
                ],
            )
        previous = session.execute(select(current.c.receipt_id)).scalar_one_or_none()
        if previous is None:
            session.execute(current.insert().values(id="current", receipt_id=identity))
        else:
            old = session.execute(
                select(table(name).c.payload).where(table(name).c.id == previous)
            ).scalar_one()
            if data["recorded_at_utc"] >= json.loads(old)["recorded_at_utc"]:
                session.execute(update(current).values(receipt_id=identity))
        dirty(session, pending(session), available_at=decode_utc(data["recorded_at_utc"]))
    elif name == "core_reconciliation_proofs":
        dirty(session, pending(session), available_at=decode_utc(data["recorded_at_utc"]))
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
        dirty(
            session,
            session.execute(query).scalars(),
            available_at=decode_utc(data["recorded_at_utc"]),
        )


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


def revoked_proof(session: Session, proof_id: str, *, available_at: datetime | None = None) -> None:
    trips = table("core_round_trips")
    query = indexed(session, "core_round_trips", reference=proof_id).with_only_columns(trips.c.id)
    dirty(session, session.execute(query).scalars(), available_at=available_at)
    dirty(session, pending(session), available_at=available_at)
