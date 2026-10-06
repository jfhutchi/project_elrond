"""Operational sidecars for explicitly registered new PAPER lots.

Protection uses only its operational database, not the experiment coordinator or
its store. Unknown ownership returns no attribution and never blocks protection.
Existing deadline documents and their hashes are unchanged.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from quantbot.domain import OrderIntent, OrderSide
from quantbot.execution.snapshot_capture import Capture, validate_capture_freshness
from quantbot.research.manifest import content_id
from quantbot.storage.database import decode_utc
from quantbot.storage.repositories import StorageRepository
from quantbot.storage.schema import (
    broker_orders,
    fills,
    order_events,
    order_intents,
    position_protection_actions,
    position_protection_evaluations,
)

if TYPE_CHECKING:
    from quantbot.learner.paper_deadlines import PaperDeadline

REGISTRATION = "ADAPTIVE_PAPER_NEW_LOT_V1"
PROTECTIVE_EXIT = "ADAPTIVE_PAPER_PROTECTION_EXIT_V1"


def register_new_lot(
    session: Session,
    record: PaperDeadline,
    capture: Capture,
    *,
    run_id: str,
    experiment_id: str,
    decision_id: str,
    now: datetime,
    clock_healthy: bool,
    synthetic: bool,
) -> str:
    """Called before entry intent persistence; fresh flat capture proves non-adoption.

    This is attribution evidence, never reconciliation or entry approval. The
    gateway must independently enforce PAPER account/mode and all risk controls.
    """
    record = type(record).model_validate(record.model_dump())
    repository = StorageRepository(session)
    if repository.get_order_intent(record.entry_intent.intent_id) is not None:
        raise ValueError("ENTRY_ALREADY_DURABLE")
    reason = validate_capture_freshness(capture, now=now, clock_healthy=clock_healthy)
    if reason or capture.snapshot is None or not capture.attempts:
        raise ValueError(reason or "CAPTURE_UNAVAILABLE")
    last = capture.attempts[-1]
    if (
        last.before is None
        or last.before != last.after
        or last.positions is None
        or last.positions != capture.snapshot.positions
        or last.before.account != capture.snapshot.account
    ):
        raise ValueError("CAPTURE_INTEGRITY_UNVERIFIED")
    if content_id({"paper_account": capture.snapshot.account.account_id}) != record.account_id_hash:
        raise ValueError("LOT_ACCOUNT_MISMATCH")
    if any(
        position.symbol == record.entry_intent.symbol and position.quantity != 0
        for position in capture.snapshot.positions
    ):
        raise ValueError("EXISTING_POSITION_CANNOT_BE_ADOPTED")
    run = repository.get_run(run_id)
    if (
        run is None
        or run.strategy_id != record.identity.strategy_id
        or repository.get_strategy_deployment(record.identity.strategy_id, record.identity.version)
        != record.identity
        or now != record.recorded_at
        or now != record.entry_intent.created_at
        or not experiment_id
        or len(decision_id) != 64
        or any(c not in "0123456789abcdef" for c in decision_id)
    ):
        raise ValueError("LOT_REGISTRATION_LINEAGE_MISMATCH")
    lot_id = content_id(
        {
            "experiment_id": experiment_id,
            "decision_id": decision_id,
            "handoff_id": record.handoff_id,
        }
    )
    document = {
        "lot_id": lot_id,
        "experiment_id": experiment_id,
        "decision_id": decision_id,
        "handoff_id": record.handoff_id,
        "entry_intent_id": record.entry_intent.intent_id,
        "handoff_transport_hash": content_id(record.model_dump(mode="json")),
        "account_id_hash": record.account_id_hash,
        "symbol": record.entry_intent.symbol,
        "registered_at": now.isoformat(),
        "position_before_entry": "0",
        "capture_hash": content_id(capture.evidence()),
        "synthetic": synthetic,
    }
    repository.record_signal(
        "adaptive-lot-" + lot_id,
        run_id=run_id,
        strategy_id=record.identity.strategy_id,
        symbol=record.entry_intent.symbol,
        occurred_at=now,
        payload={"kind": REGISTRATION, "document": document, "document_hash": content_id(document)},
    )
    return lot_id


def registrations(session: Session, *, symbol: str, now: datetime) -> tuple[dict[str, Any], ...]:
    result = []
    for signal in StorageRepository(session).list_signals(symbol=symbol):
        if signal.payload.get("kind") != REGISTRATION or signal.occurred_at > now:
            continue
        document = signal.payload.get("document")
        if not isinstance(document, dict) or signal.payload.get("document_hash") != content_id(
            document
        ):
            continue
        if (
            signal.signal_id != "adaptive-lot-" + str(document.get("lot_id"))
            or document.get("symbol") != symbol
            or document.get("registered_at") != signal.occurred_at.isoformat()
            or document.get("position_before_entry") != "0"
        ):
            continue
        result.append(document)
    return tuple(result)


def link_protection_exit(
    session: Session,
    intent: OrderIntent,
    *,
    account_id: str,
    position_quantity: Decimal,
    reconciled: bool,
    now: datetime,
) -> str | None:
    try:
        return _link_protection_exit(
            session,
            intent,
            account_id=account_id,
            position_quantity=position_quantity,
            reconciled=reconciled,
            now=now,
        )
    except (ValueError, TypeError, KeyError, ArithmeticError):
        # Corrupt attribution evidence cannot prevent an ordinary protective exit.
        # Financial accounting remains incomplete and exposes the missing link.
        return None


def _link_protection_exit(
    session: Session,
    intent: OrderIntent,
    *,
    account_id: str,
    position_quantity: Decimal,
    reconciled: bool,
    now: datetime,
) -> str | None:
    """Persist exact ownership before dispatch; ambiguity leaves accounting unknown."""
    if (
        not reconciled
        or intent.side is not OrderSide.SELL
        or position_quantity <= 0
        or intent.quantity != position_quantity
    ):
        return None
    repository = StorageRepository(session)
    action = (
        session.execute(
            select(position_protection_actions).where(
                position_protection_actions.c.intent_id == intent.intent_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if action is None or action["account_id"] != account_id:
        return None
    evaluation = (
        session.execute(
            select(position_protection_evaluations).where(
                position_protection_evaluations.c.evaluation_id == action["trigger_evaluation_id"]
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        evaluation is None
        or evaluation["reconciled"] != 1
        or evaluation["strategy_id"] != intent.strategy_id
        or evaluation["account_id"] != account_id
        or evaluation["symbol"] != intent.symbol
        or Decimal(evaluation["quantity"]) != position_quantity
    ):
        return None
    eligible: list[dict[str, Any]] = []
    for document in registrations(session, symbol=intent.symbol, now=now):
        if document.get("account_id_hash") != content_id({"paper_account": account_id}):
            continue
        signal = repository.get_signal(
            "paper-deadline-" + content_id({"intent_id": str(document.get("entry_intent_id"))})
        )
        if signal is None:
            continue
        handoff = signal.payload.get("document")
        if (
            not isinstance(handoff, dict)
            or signal.payload.get("kind") != "LEARNER_PAPER_DEADLINE_V1"
            or signal.payload.get("document_hash") != document.get("handoff_id")
            or document.get("handoff_transport_hash") != content_id(handoff)
        ):
            continue
        entry_document = handoff.get("entry_intent")
        if not isinstance(entry_document, dict):
            continue
        entry_record = OrderIntent.model_validate(entry_document)
        if (
            entry_record.intent_id != document.get("entry_intent_id")
            or entry_record.symbol != intent.symbol
            or entry_record.strategy_id != signal.strategy_id
            or entry_record.created_at < signal.occurred_at
        ):
            continue
        owned_exits = set()
        for event in session.execute(
            select(order_events).where(
                order_events.c.event_type.in_(("LEARNER_PAPER_DEADLINE_EXIT_V1", PROTECTIVE_EXIT))
            )
        ).mappings():
            detail = json.loads(event["detail_json"])
            if detail.get("handoff_id") == document["handoff_id"]:
                owned_exits.add(event["intent_id"])
        ledger = (
            session.execute(
                select(fills, order_intents.c.intent_id)
                .join(broker_orders, fills.c.broker_order_id == broker_orders.c.broker_order_id)
                .join(
                    order_intents,
                    broker_orders.c.client_order_id == order_intents.c.client_order_id,
                )
                .where(fills.c.symbol == intent.symbol)
            )
            .mappings()
            .all()
        )
        visible = [
            row for row in ledger if signal.occurred_at <= decode_utc(row["occurred_at"]) <= now
        ]
        entry = entry_record.intent_id
        if any(
            row["intent_id"] != entry and row["intent_id"] not in owned_exits for row in visible
        ):
            continue
        residual = sum(
            (Decimal(row["quantity"]) * (1 if row["side"] == "BUY" else -1) for row in visible),
            Decimal(0),
        )
        if residual == position_quantity:
            eligible.append(document)
    if len(eligible) != 1:
        return None
    document = eligible[0]
    repository.record_order_event(
        "adaptive-protection-link-" + intent.intent_id,
        event_type=PROTECTIVE_EXIT,
        occurred_at=now,
        intent_id=intent.intent_id,
        detail={
            "handoff_id": document["handoff_id"],
            "lot_id": document["lot_id"],
            "account_id_hash": document["account_id_hash"],
            "exit_strategy_id": intent.strategy_id,
            "quantity": str(intent.quantity),
            "reconciled": True,
            "registration_hash": content_id(document),
        },
    )
    return str(document["lot_id"])
