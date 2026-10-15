"""Append-only scoped health, independent of the historical kill-switch ledger."""

from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from quantbot.paper_core.contracts import CashActivity, Health, ReconciliationProof
from quantbot.paper_core.store import CoreStore, latest_certification, put, table
from quantbot.storage import StorageRepository


def active_health(session: Session) -> tuple[Health, ...]:
    events = sorted(
        (
            Health.model_validate_json(p)
            for p in session.execute(select(table("core_health_events").c.payload)).scalars()
        ),
        key=lambda h: h.sequence,
    )
    resolved = {identity for h in events for identity in h.recovered_from}
    return tuple(h for h in events if h.state != "READY" and h.id not in resolved)


def record_health(store: CoreStore, event: Health) -> str:
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        existing = [
            Health.model_validate_json(p)
            for p in session.execute(select(table("core_health_events").c.payload)).scalars()
        ]
        event = event.model_copy(
            update={"sequence": max((h.sequence for h in existing), default=0) + 1}
        )
        put(session, "core_health_events", event.id, event)
    return event.id


def report(
    store: CoreStore,
    *,
    role: Literal["TRADER", "GUARDIAN", "OUTCOMES"],
    state: Literal["TEMPORARY_WAIT", "RETRYABLE_FAILURE", "PROTOCOL_FAILURE", "INTEGRITY_FAILURE"],
    reasons: tuple[str, ...],
    now: datetime,
    scope: Literal["ACCOUNT", "SYMBOL", "LOT", "LEARNING"] = "SYMBOL",
    scope_id: str | None = None,
    evidence_ids: tuple[str, ...] = (),
    protection_blocked: bool = False,
) -> str:
    with store.database.transaction() as session:
        previous = [
            h
            for h in active_health(session)
            if h.role == role
            and h.scope == scope
            and h.scope_id == (scope_id or store.config.symbol)
            and h.state == state
        ]
        latest = latest_certification(session)
    first = min((h.first_seen for h in previous), default=now)
    attempt = len(previous)
    delay = min(15, 2 ** min(attempt, 4))
    return record_health(
        store,
        Health(
            recorded_at_utc=now,
            evidence_lane=store.config.evidence_lane,
            release_id=store.config.release_id,
            role=role,
            scope=scope,
            scope_id=scope_id or store.config.symbol,
            state=state,
            objections=reasons,
            first_blocker=reasons[0] if reasons else None,
            first_seen=first,
            last_seen=now,
            evidence_ids=evidence_ids,
            next_retry=now + timedelta(seconds=delay),
            failed_generation=latest[0] if latest else 0,
            protection_blocked=protection_blocked,
            escalated=(now - first).total_seconds() >= 300,
            recovery_requirements=(
                "NEW_COMPLETE_PROOF",
                "FRESH_TIME_AND_DEPENDENCIES",
                "GUARDIAN_ACK",
                "NO_IN_SCOPE_PAUSE_OR_QUARANTINE",
            ),
        ),
    )


def entry_health_objections(store: CoreStore, session: Session, symbol: str) -> tuple[str, ...]:
    reasons: list[tuple[int, str]] = []
    if StorageRepository(session).get_kill_switch_state().engaged:
        reasons.append((0, "CORE_MANUAL_ENTRY_PAUSE"))
    priority = {
        "INTEGRITY_FAILURE": 1,
        "MANUAL_PAUSE": 0,
        "PROTOCOL_FAILURE": 2,
        "RETRYABLE_FAILURE": 3,
        "TEMPORARY_WAIT": 4,
    }
    for event in sorted(active_health(session), key=lambda h: priority[h.state]):
        if event.role in {"TRADER", "GUARDIAN"} and (
            event.scope == "ACCOUNT"
            and event.scope_id == store.config.account_id
            or event.scope == "SYMBOL"
            and event.scope_id == symbol
        ):
            reasons.extend((priority[event.state], objection) for objection in event.objections)
    repo = StorageRepository(session)
    for incident in repo.list_incidents(unresolved_only=True):
        if incident.kind != "CORE_EVIDENCE_CONFLICT":
            continue
        lots = incident.detail.get("lot_ids")
        if incident.detail.get("account_scope") or not isinstance(lots, list) or not lots:
            reasons.append((1, "CORE_EVIDENCE_QUARANTINED"))
            continue
        authorities = incident.detail.get("disputed_authorities")
        if authorities != ["OUTCOME"] and not (
            authorities == ["CASH"] and independent_cash_available(store, session)
        ):
            reasons.append((1, "CORE_EVIDENCE_QUARANTINED"))
        # An unrelated completed outcome cannot revoke current independently proven cash.
        for link in session.execute(select(table("core_intent_links"))).mappings():
            if link["role"] != "ENTRY" or link["lot_id"] not in lots:
                continue
            completed = session.execute(
                select(table("core_round_trips").c.id).where(
                    table("core_round_trips").c.id == link["lot_id"]
                )
            ).first()
            intent = repo.get_order_intent(link["intent_id"])
            if not completed and intent and intent.symbol == symbol:
                reasons.append((1, "CORE_EVIDENCE_QUARANTINED"))
    return tuple(dict.fromkeys(reason for _, reason in sorted(reasons, key=lambda item: item[0])))


def independent_cash_available(store: CoreStore, session: Session) -> bool:
    fills = StorageRepository(session).list_fills()
    sources = table("core_broker_event_sources")
    disputed: set[str] = set()
    for incident in StorageRepository(session).list_incidents(unresolved_only=True):
        source_ids = incident.detail.get("implicated_source_ids", [])
        if incident.kind == "CORE_EVIDENCE_CONFLICT" and isinstance(source_ids, list):
            disputed.update(str(s) for s in source_ids)
    index = table("core_document_index")
    independent = set(
        session.execute(
            select(index.c.reference)
            .join(sources, sources.c.id == index.c.id)
            .where(index.c.kind == "core_cash_activity_sources", index.c.id.not_in(disputed))
        ).scalars()
    )
    complete = [
        a
        for a in (
            CashActivity.model_validate_json(p)
            for p in session.execute(select(table("core_cash_activities").c.payload)).scalars()
        )
        if a.complete
        and a.account_id == store.config.account_id
        and set(dict(a.movements)) == {f.fill_id for f in fills}
    ]
    authority = max(complete, key=lambda a: (a.watermark, a.id), default=None)
    return authority is not None and authority.id in independent


def universe_health_objections(store: CoreStore, session: Session) -> tuple[str, ...]:
    """Entry readiness requires at least one healthy member; claims check its exact scope."""
    scoped = [entry_health_objections(store, session, symbol) for symbol in store.entry_symbols]
    if any(not reasons for reasons in scoped):
        return ()
    return tuple(dict.fromkeys(reason for reasons in scoped for reason in reasons))


def recover_health(
    store: CoreStore, proof: ReconciliationProof, *, now: datetime, symbol: str | None = None
) -> None:
    symbol = symbol or store.config.symbol
    with store.database.transaction() as session:
        failures = [
            h
            for h in active_health(session)
            if h.state in {"TEMPORARY_WAIT", "RETRYABLE_FAILURE", "PROTOCOL_FAILURE"}
            and h.role in {"TRADER", "GUARDIAN"}
            and (
                h.scope == "SYMBOL"
                and h.scope_id == symbol
                or h.scope == "ACCOUNT"
                and h.scope_id == store.config.account_id
            )
            and proof.generation > h.failed_generation
            and proof.id not in h.evidence_ids
        ]
        from quantbot.paper_core.funds import entry_inventory_objections

        if entry_inventory_objections(store, session, proof, now=now):
            return
        # Quarantines remain until an explicit audited resolver supplies authority.
        if any(
            h.state in {"INTEGRITY_FAILURE", "MANUAL_PAUSE"}
            and (
                h.scope == "ACCOUNT"
                and h.scope_id == store.config.account_id
                or h.scope == "SYMBOL"
                and h.scope_id == symbol
            )
            for h in active_health(session)
        ):
            return
        if StorageRepository(session).get_kill_switch_state().engaged:
            return
    if failures:
        record_health(
            store,
            Health(
                recorded_at_utc=now,
                evidence_lane=store.config.evidence_lane,
                release_id=store.config.release_id,
                role="TRADER",
                scope="SYMBOL",
                scope_id=symbol,
                state="READY",
                objections=(),
                first_blocker=None,
                first_seen=now,
                last_seen=now,
                last_useful_progress=now,
                new_proof_id=proof.id,
                evidence_ids=(proof.id,),
                recovered_from=tuple(h.id for h in failures),
            ),
        )
