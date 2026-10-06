"""Snapshot-consistent operational fill import; never broker calls or runtime writes."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from quantbot.learner.experiment_contract import ExperimentManifest
from quantbot.learner.experiment_lots import LotEvent, journal_lot_outcome, record_lot_event
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.paper_deadlines import CLOSED_EVENT, EXIT_EVENT, HEARTBEAT
from quantbot.learner.trade_learning import _episode, _time
from quantbot.research.manifest import content_id
from quantbot.storage.lot_ownership import PROTECTIVE_EXIT, REGISTRATION


def refresh_lot_journal(
    store: ExperimentStore, manifest: ExperimentManifest, *, operational_path: Path, now: datetime
) -> tuple[LotEvent, ...]:
    declarations = [
        event.decode(LotEvent)
        for event in store.read_as_of(manifest.experiment_id, now)
        if event.kind == "LotEvent" and event.decode(LotEvent).kind == "ENTRY_DECLARED"
    ]
    result: list[LotEvent] = []
    with sqlite3.connect(operational_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        for declared in declarations:
            registration = db.execute(
                "SELECT * FROM signals WHERE signal_id=?", ("adaptive-lot-" + declared.lot_id,)
            ).fetchone()
            if registration is None:
                raise ValueError("OPERATIONAL_LOT_REGISTRATION_MISSING")
            payload = json.loads(registration["payload_json"])
            document = payload.get("document")
            if (
                not isinstance(document, dict)
                or payload.get("kind") != REGISTRATION
                or payload.get("document_hash") != content_id(document)
                or document.get("experiment_id") != manifest.experiment_id
                or document.get("decision_id") != declared.decision_id
                or document.get("account_id_hash") != declared.account_id_hash
                or document.get("synthetic") != manifest.synthetic
            ):
                raise ValueError("OPERATIONAL_LOT_LINEAGE_MISMATCH")
            signal = db.execute(
                "SELECT * FROM signals WHERE signal_id=?",
                ("paper-deadline-" + content_id({"intent_id": declared.order_id}),),
            ).fetchone()
            if signal is None:
                raise ValueError("OPERATIONAL_HANDOFF_MISSING")
            episode = _episode(db, signal, now)
            if episode.handoff_id != document.get("handoff_id"):
                raise ValueError("OPERATIONAL_HANDOFF_MISMATCH")
            existing = [
                event.decode(LotEvent)
                for event in store.read_as_of(manifest.experiment_id, now)
                if event.kind == "LotEvent" and event.decode(LotEvent).lot_id == declared.lot_id
            ]

            def append(
                kind: str,
                *,
                occurred_at: datetime,
                source_hash: str,
                declared: LotEvent = declared,
                existing: list[LotEvent] = existing,
                **values: object,
            ) -> None:
                candidate = LotEvent.model_validate(
                    {
                        **declared.model_dump(),
                        "kind": kind,
                        "recorded_at": now,
                        "occurred_at": occurred_at,
                        "source_hash": source_hash,
                        "position_before_entry": None,
                        **values,
                    }
                )
                # Terminal observations have an actual import time; unchanged broker
                # evidence retains its first receipt instead of generating fake revisions.
                if any(
                    row.kind == candidate.kind
                    and row.order_id == candidate.order_id
                    and row.source_hash == candidate.source_hash
                    for row in existing
                ):
                    return
                if record_lot_event(store, candidate):
                    result.append(candidate)
                    existing.append(candidate)

            imported_fills = []
            for fill_id in (*episode.entry_fill_ids, *episode.exit_fill_ids):
                row = db.execute(
                    "SELECT f.*, i.intent_id, i.quantity AS intent_quantity, i.strategy_id, "
                    "i.client_order_id FROM fills f JOIN broker_orders b USING(broker_order_id) "
                    "JOIN order_intents i USING(client_order_id) WHERE f.fill_id=?",
                    (fill_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("VERIFIED_FILL_DISAPPEARED")
                owner, linked_at = "ENTRY", declared.occurred_at
                if row["side"] == "SELL":
                    ownership = [
                        event
                        for event in db.execute(
                            "SELECT * FROM order_events WHERE intent_id=? AND event_type IN (?,?)",
                            (row["intent_id"], EXIT_EVENT, PROTECTIVE_EXIT),
                        )
                        if json.loads(event["detail_json"]).get("handoff_id") == episode.handoff_id
                        and _time(event["occurred_at"]) <= _time(row["occurred_at"])
                    ]
                    if len(ownership) != 1:
                        raise ValueError("EXIT_OWNERSHIP_UNVERIFIED")
                    owner = (
                        "PROTECTION"
                        if ownership[0]["event_type"] == PROTECTIVE_EXIT
                        else "DEADLINE"
                    )
                    linked_at = _time(ownership[0]["occurred_at"])
                link_hash = content_id(
                    {
                        "intent": row["intent_id"],
                        "side": row["side"],
                        "strategy": row["strategy_id"],
                        "quantity": row["intent_quantity"],
                        "owner": owner,
                    }
                )
                links = [
                    event
                    for event in existing
                    if event.kind == "ORDER_LINK" and event.order_id == row["intent_id"]
                ]
                if links and any(
                    event.owner != owner
                    or event.side != row["side"]
                    or event.quantity != Decimal(row["intent_quantity"])
                    for event in links
                ):
                    raise ValueError("OPERATIONAL_OWNERSHIP_MISMATCH")
                if not links:
                    append(
                        "ORDER_LINK",
                        occurred_at=linked_at,
                        source_hash=link_hash,
                        order_id=row["intent_id"],
                        side=row["side"],
                        owner=owner,
                        quantity=row["intent_quantity"],
                    )
                fill_document = {
                    key: row[key]
                    for key in (
                        "fill_id",
                        "broker_order_id",
                        "symbol",
                        "side",
                        "quantity",
                        "price",
                        "fee",
                        "occurred_at",
                    )
                }
                append(
                    "FILL",
                    occurred_at=_time(row["occurred_at"]),
                    source_hash=content_id(fill_document),
                    order_id=row["intent_id"],
                    side=row["side"],
                    owner=owner,
                    fill_id=fill_id,
                    quantity=row["quantity"],
                    price=row["price"],
                    fee=row["fee"],
                )
                imported_fills.append(row)
            for order_id in {row["intent_id"] for row in imported_fills}:
                order = db.execute(
                    "SELECT b.* FROM broker_orders b JOIN order_intents i "
                    "USING(client_order_id) WHERE i.intent_id=?",
                    (order_id,),
                ).fetchone()
                if order is not None and order["status"] in {
                    "filled",
                    "canceled",
                    "expired",
                    "rejected",
                }:
                    append(
                        "ORDER_TERMINAL",
                        occurred_at=now,
                        source_hash=content_id(dict(order)),
                        order_id=order_id,
                        quantity=order["filled_quantity"],
                        terminal=True,
                    )
            for signal_row in db.execute(
                "SELECT s.*, r.status AS run_status, r.started_at "
                "FROM signals s JOIN runs r USING(run_id) WHERE s.symbol='PAPER_DEADLINES'"
            ):
                heartbeat = json.loads(signal_row["payload_json"])
                stamp = _time(signal_row["occurred_at"])
                if (
                    heartbeat.get("kind") != HEARTBEAT
                    or heartbeat.get("healthy") is not True
                    or heartbeat.get("account_id_hash") != declared.account_id_hash
                    or signal_row["run_status"] != "SUCCEEDED"
                    or not declared.occurred_at <= _time(signal_row["started_at"]) <= stamp <= now
                ):
                    continue
                quantity = sum(
                    (
                        Decimal(row["quantity"]) * (1 if row["side"] == "BUY" else -1)
                        for row in imported_fills
                        if _time(row["occurred_at"]) <= stamp
                    ),
                    Decimal(0),
                )
                if quantity > 0:
                    append(
                        "PROTECTION_BOUND",
                        occurred_at=stamp,
                        quantity=quantity,
                        source_hash=content_id(
                            {
                                "heartbeat": dict(signal_row),
                                "quantity": quantity,
                                "fill_ids": sorted(
                                    row["fill_id"]
                                    for row in imported_fills
                                    if _time(row["occurred_at"]) <= stamp
                                ),
                            }
                        ),
                    )
            if episode.status == "CLOSED":
                closures = [
                    event
                    for event in db.execute(
                        "SELECT * FROM order_events WHERE event_type=? AND intent_id=?",
                        (CLOSED_EVENT, declared.order_id),
                    )
                    if json.loads(event["detail_json"]).get("handoff_id") == episode.handoff_id
                    and _time(event["occurred_at"]) <= now
                ]
                for closure in closures:
                    append(
                        "RECONCILED_FLAT",
                        occurred_at=_time(closure["occurred_at"]),
                        source_hash=content_id(dict(closure)),
                        quantity=0,
                        reconciled=True,
                    )
            journal_lot_outcome(store, declared.lot_id, now=now)
    return tuple(result)
