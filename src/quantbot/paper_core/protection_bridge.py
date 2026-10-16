"""Read-only ownership bridge; retained protection never acquires a core-owned exit."""

import os
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from quantbot.domain import Position
from quantbot.paper_core.authority import RoleHeartbeat, RuntimeCommission, executable_in_session
from quantbot.paper_core.contracts import CoreConfig, ProtectionPlan, RoundTrip
from quantbot.paper_core.send_guard import definitively_unsent
from quantbot.paper_core.store import table
from quantbot.paper_core.work import current_receipts
from quantbot.research.manifest import content_id
from quantbot.storage import StorageRepository


def core_reserves_symbol(session: Session, symbol: str) -> tuple[dict[str, Any], ...]:
    repo = StorageRepository(session)
    links = table("core_intent_links")
    rows = session.execute(
        select(links)
        .join(table("order_intents"), table("order_intents").c.intent_id == links.c.intent_id)
        .where(links.c.role == "ENTRY", table("order_intents").c.symbol == symbol)
    ).mappings()
    active = []
    for link in rows:
        if definitively_unsent(repo, link["intent_id"]):
            continue
        outcome = session.execute(
            select(table("core_round_trips").c.payload).where(
                table("core_round_trips").c.id == link["lot_id"]
            )
        ).scalar_one_or_none()
        if outcome and RoundTrip.model_validate_json(outcome).open_quantity == 0:
            continue
        active.append(dict(link))
    return tuple(active)


def core_position_protection(
    session: Session,
    *,
    account_id: str,
    position: Position,
    now: datetime,
) -> tuple[bool, bool, tuple[str, ...]]:
    """Return (core-reserved, freshly delegated, evidence IDs), never execution authority."""
    links = core_reserves_symbol(session, position.symbol)
    if not links:
        return False, False, ()
    # Ambiguous/mixed ownership is visible but cannot transfer to the retained owner.
    if len(links) != 1:
        return True, False, ()
    link = links[0]
    from quantbot.paper_core.health import active_health

    blocked = tuple(
        h.id
        for h in active_health(session)
        if (
            h.state == "INTEGRITY_FAILURE"
            and h.protection_blocked
            and (
                h.scope == "ACCOUNT"
                and h.scope_id == account_id
                or h.scope == "SYMBOL"
                and h.scope_id == position.symbol
                or h.scope == "LOT"
                and h.scope_id == link["lot_id"]
            )
        )
    )
    if blocked:
        # A previous healthy heartbeat cannot override a newly recorded quarantine.
        return True, False, blocked
    try:
        encoded = session.execute(
            select(table("core_runtime_commissions").c.payload)
        ).scalar_one_or_none()
        config_payload = session.execute(
            select(table("core_identities").c.payload)
        ).scalar_one_or_none()
        binding = executable_in_session(session)
        if encoded is None or config_payload is None or binding is None:
            return True, False, ()
        commission = RuntimeCommission.model_validate_json(encoded)
        config = CoreConfig.model_validate_json(config_payload)
        if (
            commission.account_id != account_id
            or config.account_id != account_id
            or commission.config_id != content_id(config)
            or binding.commission_id != commission.id
        ):
            return True, False, ()
        payload = session.execute(
            select(table("core_protection_plans").c.payload).where(
                table("core_protection_plans").c.id == link["protection_id"]
            )
        ).scalar_one()
        plan = ProtectionPlan.model_validate_json(payload)
        repo = StorageRepository(session)
        intent = repo.get_order_intent(link["intent_id"])
        if (
            plan.id != link["protection_id"]
            or intent is None
            or intent.strategy_id != config.owner
            or plan.owner != config.owner
            or plan.generation != config.generation
        ):
            return True, False, ()
        order = repo.get_broker_order_by_client_order_id(intent.client_order_id)
        buys = () if order is None else repo.list_fills(broker_order_id=order.broker_order_id)
        owned = sum((f.quantity for f in buys), Decimal(0))
        basis = sum((f.quantity * f.price for f in buys), Decimal(0)) / owned if owned else None
        for exit_link in session.execute(
            select(table("core_intent_links")).where(
                table("core_intent_links").c.lot_id == link["lot_id"],
                table("core_intent_links").c.role == "EXIT",
            )
        ).mappings():
            exit_intent = repo.get_order_intent(exit_link["intent_id"])
            if exit_intent is None:
                return True, False, ()
            exit_order = repo.get_broker_order_by_client_order_id(exit_intent.client_order_id)
            if exit_order:
                owned -= sum(
                    (
                        f.quantity
                        for f in repo.list_fills(broker_order_id=exit_order.broker_order_id)
                    ),
                    Decimal(0),
                )
        if owned <= 0 or position.quantity != owned or position.average_entry_price != basis:
            return True, False, ()
        payload = session.execute(
            select(table("core_role_heartbeats").c.payload)
            .join(
                table("core_role_current"),
                table("core_role_current").c.heartbeat_id == table("core_role_heartbeats").c.id,
            )
            .where(table("core_role_current").c.role == "guardian")
        ).scalar_one_or_none()
        heartbeat = RoleHeartbeat.model_validate_json(payload) if payload else None
        receipts = current_receipts(session, plan_id=plan.id)
        receipt = receipts[0] if receipts else None
        identity = session.execute(select(table("schema_version").c.store_identity)).scalar_one()
        if (
            heartbeat is None
            or receipt is None
            or heartbeat.pid == os.getpid()
            or heartbeat.release_id != binding.release.id
            or heartbeat.config_id != commission.config_id
            or heartbeat.store_identity != identity
            or heartbeat.owner != config.owner
            or heartbeat.generation != config.generation
            or heartbeat.objections
            or not heartbeat.recorded_at <= now < heartbeat.valid_until
            or heartbeat.last_useful_progress is None
            or not 0 <= (now - heartbeat.last_useful_progress).total_seconds() <= 30
            or heartbeat.guardian_receipt_id != receipt.id
            or receipt.owner != config.owner
            or receipt.generation != config.generation
            or not receipt.recorded_at_utc <= now < receipt.valid_until
        ):
            return True, False, ()
        return True, True, (binding.id, plan.id, heartbeat.id, receipt.id)
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return True, False, ()
