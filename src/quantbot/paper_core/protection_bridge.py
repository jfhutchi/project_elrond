"""Read-only ownership bridge; retained protection never acquires a core-owned exit."""

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, NamedTuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from quantbot.domain import Position
from quantbot.paper_core.authority import RoleHeartbeat, RuntimeCommission
from quantbot.paper_core.contracts import CoreConfig, GuardianReceipt, ProtectionPlan, RoundTrip
from quantbot.paper_core.settlement import definitively_unsent, settled_zero_for_config
from quantbot.paper_core.store import table
from quantbot.research.manifest import content_id
from quantbot.storage import StorageRepository


class ProtectionBinding(NamedTuple):
    id: str
    commission_id: str
    release_id: str


def protection_binding(session: Session) -> ProtectionBinding | None:
    """Read the sealed current identity without interpreting the entry engine's config.

    Retained protection grants no core execution authority. Its older process may not
    understand a newer admission contract, but must still validate the exact binding,
    PAPER endpoint, lineage, commission and current independent Guardian below.
    """
    history, pointer = table("core_runtime_config_history"), table("core_runtime_current")
    row = session.execute(
        select(history.c.id, history.c.payload).join(pointer, pointer.c.document_id == history.c.id)
    ).one_or_none()
    if row is None:
        return None
    document = json.loads(row[1])
    release = document["release"]
    runtime = release["config"]
    schema = session.execute(
        select(table("schema_version").c.version, table("schema_version").c.store_identity)
    ).one()
    policy = runtime.get("entry_policy")
    if (
        content_id(document) != row[0]
        or document.get("kind", "CODE_ONLY_EXECUTABLE_BINDING_V1")
        not in {"CODE_ONLY_EXECUTABLE_BINDING_V1", "ENTRY_POLICY_EXECUTABLE_BINDING_V1"}
        or runtime["version"] != "paper-core-runtime-v1"
        or runtime["endpoint"] != "https://paper-api.alpaca.markets"
        or runtime["store_identity"] != schema[1]
        or release["schema_version"] != schema[0]
        or policy is not None
        and policy["contract"] not in {"WHOLE_SHARE_FALLBACK_V1", "RESOURCE_BOUNDED_PAPER_V1"}
    ):
        raise ValueError("RETAINED_PROTECTION_BINDING_INVALID")
    return ProtectionBinding(row[0], document["commission_id"], content_id(release))


def core_reserves_symbol(
    session: Session, symbol: str, *, now: datetime | None = None
) -> tuple[dict[str, Any], ...]:
    repo = StorageRepository(session)
    links = table("core_intent_links")
    rows = session.execute(
        select(links)
        .join(table("order_intents"), table("order_intents").c.intent_id == links.c.intent_id)
        .where(links.c.role == "ENTRY", table("order_intents").c.symbol == symbol)
    ).mappings()
    active = []
    config_payload = session.execute(
        select(table("core_identities").c.payload)
    ).scalar_one_or_none()
    config = CoreConfig.model_validate_json(config_payload) if config_payload else None
    for link in rows:
        if definitively_unsent(repo, link["intent_id"]):
            continue
        intent = repo.get_order_intent(link["intent_id"])
        if (
            config is not None
            and intent is not None
            and settled_zero_for_config(config, session, intent, cutoff=now or datetime.now(UTC))
            is not None
        ):
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
    clock: Callable[[], datetime] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> tuple[bool, bool, tuple[str, ...]]:
    """Read one exact Guardian publication; never grant retained execution authority.

    The caller's time precedes database work. Recheck against its advancing clock
    after acquisition, and reread a changed publication once without a writer lock.
    """
    detail: dict[str, Any] = dict(checked_at=now.isoformat(), refusal_codes=[])

    def result(
        reserved: bool,
        delegated: bool,
        codes: tuple[str, ...] = (),
        evidence: tuple[str, ...] = (),
    ) -> tuple[bool, bool, tuple[str, ...]]:
        detail.update(reserved=reserved, delegated=delegated, refusal_codes=list(codes))
        if diagnostics is not None:
            diagnostics.update(detail)
        return reserved, delegated, tuple(dict.fromkeys(evidence))

    links = core_reserves_symbol(session, position.symbol, now=now)
    if not links:
        return result(False, False, ("NO_CORE_RESERVATION",))
    if len(links) != 1:
        return result(True, False, ("AMBIGUOUS_CORE_OWNERSHIP",))
    link = links[0]
    detail.update(lot_id=link["lot_id"], plan_id=link["protection_id"])
    from quantbot.paper_core.health import active_health

    def quarantine() -> tuple[str, ...]:
        return tuple(
            h.id
            for h in active_health(session)
            if h.state == "INTEGRITY_FAILURE"
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

    blocked = quarantine()
    if blocked:
        detail["blocking_health_ids"] = list(blocked)
        return result(True, False, ("PROTECTION_QUARANTINED",), blocked)
    try:
        encoded = session.execute(
            select(table("core_runtime_commissions").c.payload)
        ).scalar_one_or_none()
        config_payload = session.execute(
            select(table("core_identities").c.payload)
        ).scalar_one_or_none()
        binding = protection_binding(session)
        if encoded is None or config_payload is None or binding is None:
            return result(True, False, ("COMMISSION_CONFIG_OR_BINDING_MISSING",))
        commission = RuntimeCommission.model_validate_json(encoded)
        config = CoreConfig.model_validate_json(config_payload)
        detail.update(binding_id=binding.id, commission_id=commission.id)
        if (
            commission.account_id != account_id
            or config.account_id != account_id
            or commission.config_id != content_id(config)
            or binding.commission_id != commission.id
        ):
            return result(True, False, ("COMMISSION_IDENTITY_MISMATCH",), (binding.id,))
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
            return result(True, False, ("ORIGINAL_PLAN_OR_OWNER_MISMATCH",), (binding.id,))
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
                return result(True, False, ("EXIT_INTENT_MISSING",), (binding.id, plan.id))
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
            return result(True, False, ("OWNED_QUANTITY_OR_BASIS_MISMATCH",), (binding.id, plan.id))
        identity = session.execute(select(table("schema_version").c.store_identity)).scalar_one()
        heartbeats, roles, receipts = (
            table(n)
            for n in ("core_role_heartbeats", "core_role_current", "core_guardian_receipts")
        )
        pairs: list[dict[str, str | None]] = []
        detail["observed_publications"] = pairs
        for attempt in range(2):
            for key in (
                "heartbeat_id",
                "receipt_id",
                "heartbeat_objections",
                "heartbeat_recorded_at",
                "heartbeat_valid_until",
                "last_useful_progress",
                "receipt_recorded_at",
                "receipt_valid_until",
            ):
                detail.pop(key, None)
            # One SQL statement binds the current heartbeat to the receipt it names.
            # current_receipts() would reread the moving heartbeat pointer independently.
            row = (
                session.execute(
                    select(
                        heartbeats.c.id.label("heartbeat_id"),
                        heartbeats.c.payload.label("heartbeat_payload"),
                        receipts.c.id.label("receipt_id"),
                        receipts.c.payload.label("receipt_payload"),
                    )
                    .select_from(heartbeats)
                    .join(roles, roles.c.heartbeat_id == heartbeats.c.id)
                    .outerjoin(
                        receipts,
                        receipts.c.id
                        == func.json_extract(heartbeats.c.payload, "$.guardian_receipt_id"),
                    )
                    .where(roles.c.role == "guardian")
                )
                .mappings()
                .one_or_none()
            )
            if row is not None:
                detail.update(heartbeat_id=row["heartbeat_id"], receipt_id=row["receipt_id"])
                pairs.append(dict(heartbeat_id=row["heartbeat_id"], receipt_id=row["receipt_id"]))
            checked = clock() if clock is not None else now
            detail.update(checked_at=checked.isoformat(), publication_rechecks=attempt)
            if checked.tzinfo is None or checked.utcoffset() is None or checked < now:
                return result(True, False, ("AUTHORITY_CLOCK_INVALID_OR_REGRESSED",))
            if row is None or row["receipt_payload"] is None:
                return result(True, False, ("GUARDIAN_HEARTBEAT_OR_RECEIPT_MISSING",))
            heartbeat_id, receipt_id = row["heartbeat_id"], row["receipt_id"]
            heartbeat = RoleHeartbeat.model_validate_json(row["heartbeat_payload"])
            receipt = GuardianReceipt.model_validate_json(row["receipt_payload"])
            detail.update(
                heartbeat_id=row["heartbeat_id"],
                receipt_id=row["receipt_id"],
                heartbeat_objections=list(heartbeat.objections),
                heartbeat_recorded_at=heartbeat.recorded_at.isoformat(),
                heartbeat_valid_until=heartbeat.valid_until.isoformat(),
                last_useful_progress=(
                    heartbeat.last_useful_progress.isoformat()
                    if heartbeat.last_useful_progress is not None
                    else None
                ),
                receipt_recorded_at=receipt.recorded_at_utc.isoformat(),
                receipt_valid_until=receipt.valid_until.isoformat(),
            )
            evidence = (binding.id, plan.id, heartbeat.id, receipt.id)

            def authority_errors(
                at: datetime,
                heartbeat: RoleHeartbeat,
                receipt: GuardianReceipt,
                heartbeat_id: str,
                receipt_id: str,
            ) -> tuple[str, ...]:
                checks = (
                    (
                        "GUARDIAN_DOCUMENT_ID_MISMATCH",
                        heartbeat.id != heartbeat_id or receipt.id != receipt_id,
                    ),
                    ("GUARDIAN_PROCESS_NOT_INDEPENDENT", heartbeat.pid == os.getpid()),
                    ("GUARDIAN_RELEASE_MISMATCH", heartbeat.release_id != binding.release_id),
                    ("GUARDIAN_CONFIG_MISMATCH", heartbeat.config_id != commission.config_id),
                    ("GUARDIAN_STORE_MISMATCH", heartbeat.store_identity != identity),
                    (
                        "GUARDIAN_OWNER_OR_GENERATION_MISMATCH",
                        heartbeat.owner != config.owner
                        or heartbeat.generation != config.generation,
                    ),
                    ("GUARDIAN_OBJECTIONS", bool(heartbeat.objections)),
                    (
                        "GUARDIAN_HEARTBEAT_NOT_CURRENT",
                        not heartbeat.recorded_at <= at < heartbeat.valid_until,
                    ),
                    (
                        "GUARDIAN_PROGRESS_STALE_OR_FUTURE",
                        heartbeat.last_useful_progress is None
                        or not 0 <= (at - heartbeat.last_useful_progress).total_seconds() <= 30,
                    ),
                    ("GUARDIAN_RECEIPT_MISMATCH", heartbeat.guardian_receipt_id != receipt.id),
                    ("RECEIPT_PLAN_MISSING", plan.id not in receipt.protection_ids),
                    (
                        "RECEIPT_OWNER_OR_GENERATION_MISMATCH",
                        receipt.owner != config.owner or receipt.generation != config.generation,
                    ),
                    (
                        "RECEIPT_NOT_CURRENT",
                        not receipt.recorded_at_utc <= at < receipt.valid_until,
                    ),
                )
                return tuple(code for code, failed in checks if failed)

            latest = session.execute(
                select(roles.c.heartbeat_id).where(roles.c.role == "guardian")
            ).scalar_one_or_none()
            if latest != heartbeat.id:
                continue  # A newer publication must pass every guard itself, once.
            errors = authority_errors(checked, heartbeat, receipt, heartbeat_id, receipt_id)
            if errors:
                return result(True, False, errors, evidence)
            blocked = quarantine()
            if blocked:
                detail["blocking_health_ids"] = list(blocked)
                return result(True, False, ("PROTECTION_QUARANTINED",), evidence + blocked)
            final_checked = clock() if clock is not None else checked
            detail["checked_at"] = final_checked.isoformat()
            if final_checked < checked:
                return result(True, False, ("AUTHORITY_CLOCK_INVALID_OR_REGRESSED",), evidence)
            errors = authority_errors(final_checked, heartbeat, receipt, heartbeat_id, receipt_id)
            if errors:
                return result(True, False, errors, evidence)
            return result(True, True, evidence=evidence)
        return result(True, False, ("GUARDIAN_PUBLICATION_CHANGED_DURING_READ",))
    except (ValueError, TypeError, KeyError, ArithmeticError) as error:
        detail["error_type"] = type(error).__name__
        return result(True, False, ("AUTHORITY_DOCUMENT_INVALID",))
