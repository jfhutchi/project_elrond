"""Append-only scoped health, independent of the historical kill-switch ledger."""

import json
from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import Any, Literal, TypeVar

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from quantbot.paper_core.contracts import CashActivity, Evidence, Health, ReconciliationProof
from quantbot.paper_core.store import CoreStore, latest_certification, put, table
from quantbot.storage import StorageRepository

RecoveryEvidence = TypeVar("RecoveryEvidence", bound=Evidence)


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


def record_health(store: CoreStore, event: Health, *, session: Session | None = None) -> str:
    owns_session = session is None
    with store.database.transaction() if owns_session else nullcontext(session) as session:
        assert session is not None
        if owns_session:
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
    session: Session | None = None,
) -> str:
    owns_session = session is None
    with store.database.transaction() if owns_session else nullcontext(session) as session:
        assert session is not None
        if owns_session:
            session.execute(text("BEGIN IMMEDIATE"))
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
            session=session,
        )


def entry_health_objections(
    store: CoreStore,
    session: Session,
    symbol: str,
    *,
    health_events: tuple[Health, ...] | None = None,
) -> tuple[str, ...]:
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
    for event in sorted(
        active_health(session) if health_events is None else health_events,
        key=lambda h: priority[h.state],
    ):
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
    events = active_health(session)
    objections: list[str] = []
    policy = store.entry_policy
    if policy is not None and policy.dynamic:
        # Account/manual/integrity gates here; exact symbol gates remain mandatory at claim/send.
        return entry_health_objections(store, session, "__DISCOVERY__", health_events=events)
    for symbol in store.entry_symbols:
        reasons = entry_health_objections(store, session, symbol, health_events=events)
        if not reasons:
            return ()
        objections.extend(reasons)
    return tuple(dict.fromkeys(objections))


def recover_health(
    store: CoreStore, proof: ReconciliationProof, *, now: datetime, symbol: str | None = None
) -> None:
    recover_health_many(store, proof, now=now, symbols=(symbol or store.config.symbol,))


def recover_health_many(
    store: CoreStore, proof: ReconciliationProof, *, now: datetime, symbols: tuple[str, ...]
) -> None:
    """Recover one cohort against current locked authority, retaining per-symbol receipts."""
    if not symbols:
        return
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        events = sorted(
            (
                Health.model_validate_json(p)
                for p in session.execute(select(table("core_health_events").c.payload)).scalars()
            ),
            key=lambda h: h.sequence,
        )
        sequence = max((h.sequence for h in events), default=0)
        resolved = {identity for h in events for identity in h.recovered_from}
        active = tuple(h for h in events if h.state != "READY" and h.id not in resolved)
        if store.config.clock_profile == "PAPER_TIME_V2":
            now = max(now, store.clocks.sample()[0])
        from quantbot.paper_core.funds import entry_inventory_objections

        if entry_inventory_objections(store, session, proof, now=now):
            return
        if StorageRepository(session).get_kill_switch_state().engaged:
            return

        for symbol in dict.fromkeys(symbols):
            scoped = tuple(
                h
                for h in active
                if h.id not in resolved
                and (
                    h.scope == "SYMBOL"
                    and h.scope_id == symbol
                    or h.scope == "ACCOUNT"
                    and h.scope_id == store.config.account_id
                )
            )
            # Quarantines remain until an explicit audited resolver supplies authority.
            if any(h.state in {"INTEGRITY_FAILURE", "MANUAL_PAUSE"} for h in scoped):
                continue
            failures = tuple(
                h
                for h in scoped
                if h.state in {"TEMPORARY_WAIT", "RETRYABLE_FAILURE", "PROTOCOL_FAILURE"}
                and h.role in {"TRADER", "GUARDIAN"}
                and proof.generation > h.failed_generation
                and proof.id not in h.evidence_ids
            )
            if not failures:
                continue
            sequence += 1
            recovered = Health(
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
                sequence=sequence,
            )
            put(session, "core_health_events", recovered.id, recovered)
            resolved.update(recovered.recovered_from)


def resolve_delayed_owned_buy(store: CoreStore, approval: dict[str, Any]) -> str:
    """Explicitly resolve one proven delayed-first-buy quarantine; never send or adopt.

    This is deliberately narrower than health recovery: an original zero position,
    a retained stable owned acknowledgement, later canonical fills, and fresh current
    quantity/time authority must explain the entire original discrepancy atomically.
    """
    from decimal import Decimal

    from quantbot.domain import BrokerOrder, IntentState, OrderSide
    from quantbot.paper_core.authority import (
        RoleHeartbeat,
        commission_in_session,
        executable_in_session,
    )
    from quantbot.paper_core.contracts import BrokerEvent, ProtectionPlan
    from quantbot.paper_core.outcomes import _source_id
    from quantbot.paper_core.time_evidence import TimeEvidence
    from quantbot.research.manifest import content_id
    from quantbot.storage import StateConflictError

    def require(condition: bool, reason: str) -> None:
        if not condition:
            raise StateConflictError("OWNED_BUY_RECOVERY_" + reason)

    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        now = store.clocks.sample()[0]

        def load(name: str, identity: str | None, kind: type[RecoveryEvidence]) -> RecoveryEvidence:
            encoded = session.execute(
                select(table(name).c.payload).where(table(name).c.id == identity)
            ).scalar_one_or_none()
            require(encoded is not None, "EVIDENCE_MISSING")
            assert encoded is not None
            value = kind.model_validate_json(encoded)
            require(value.id == identity, "EVIDENCE_ID_MISMATCH")
            return value

        binding = executable_in_session(session)
        commission = commission_in_session(store, session)
        require(
            binding is not None
            and binding.release.id == store.runtime_release_id
            and binding.commission_id == commission.id
            and approval.get("binding_id") == binding.id
            and approval.get("runtime_release_id") == store.runtime_release_id
            and approval.get("commission_id") == commission.id
            and bool(approval.get("approval_reference")),
            "EXACT_APPROVAL_REQUIRED",
        )
        require(
            0 <= (now - datetime.fromisoformat(approval["approved_at"])).total_seconds() <= 120,
            "APPROVAL_EXPIRED",
        )
        target = load("core_health_events", approval.get("health_id"), Health)
        require(
            target.id in {h.id for h in active_health(session)}
            and target.role == "GUARDIAN"
            and target.scope == "SYMBOL"
            and target.state == "INTEGRITY_FAILURE"
            and target.protection_blocked
            and target.objections == ("INVENTORY_OWNERSHIP_CONTRADICTION",)
            and target.evidence_ids == (approval.get("failed_proof_id"),),
            "EXACT_QUARANTINE_REQUIRED",
        )
        symbol = target.scope_id
        failed = load(
            "core_reconciliation_proofs", approval["failed_proof_id"], ReconciliationProof
        )
        certified = latest_certification(session)
        require(
            certified is not None and certified[1] == approval.get("proof_id"),
            "LATEST_CERTIFIED_PROOF_REQUIRED",
        )
        assert certified is not None
        proof = load("core_reconciliation_proofs", certified[1], ReconciliationProof)
        quantities, cash, fill_ids = store.ledger(session)
        require(
            proof.generation == certified[0]
            and proof.generation > target.failed_generation
            and proof.recorded_at_utc > failed.recorded_at_utc
            and proof.complete
            and proof.account.account_id == store.config.account_id
            and proof.release_id == store.config.release_id
            and proof.baseline_id == failed.baseline_id
            and proof.actual_quantities
            == proof.expected_quantities
            == tuple(sorted(quantities.items()))
            and proof.fill_ids == fill_ids
            and proof.expected_cash == cash
            and proof.ledger_state_id == store.ledger_state_id(session)
            and 0 <= (now - proof.recorded_at_utc).total_seconds() <= 30,
            "FRESH_LEDGER_PROOF_REQUIRED",
        )
        require(
            failed.account.account_id == store.config.account_id
            and failed.release_id == store.config.release_id
            and failed.complete
            and failed.generation == target.failed_generation + 1
            and failed.recorded_at_utc <= target.recorded_at_utc
            and not failed.cash_complete
            and failed.expected_cash is None,
            "FAILED_QUANTITY_PROOF_REQUIRED",
        )
        baseline_payload = session.execute(
            select(table("core_fixture_baselines").c.payload).where(
                table("core_fixture_baselines").c.id == failed.baseline_id
            )
        ).scalar_one()
        baseline = json.loads(baseline_payload)
        require(
            not any(p["symbol"] == symbol for p in baseline["positions"])
            and dict(failed.expected_quantities).get(symbol, Decimal(0)) == 0,
            "INHERITED_OR_EXISTING_POSITION",
        )
        old_expected, old_actual = dict(failed.expected_quantities), dict(failed.actual_quantities)
        delta = {
            s: old_actual.get(s, Decimal(0)) - old_expected.get(s, Decimal(0))
            for s in old_actual.keys() | old_expected.keys()
            if old_actual.get(s, 0) != old_expected.get(s, 0)
        }
        require(
            set(delta) == {symbol}
            and delta[symbol] > 0
            and proof.expected_quantities == failed.actual_quantities,
            "UNEXPLAINED_QUANTITY_CHANGE",
        )
        old_basis = {p.symbol: p.average_entry_price for p in failed.positions}
        require(
            {p.symbol: p.average_entry_price for p in proof.positions} == old_basis,
            "POSITION_BASIS_CHANGED",
        )
        repo = StorageRepository(session)
        require(not repo.list_unresolved_order_intents(), "UNRESOLVED_SUBMISSION")
        require(
            not any(
                h.id != target.id
                and h.state in {"INTEGRITY_FAILURE", "MANUAL_PAUSE"}
                and (
                    h.scope == "ACCOUNT"
                    and h.scope_id == store.config.account_id
                    or h.scope == "SYMBOL"
                    and h.scope_id == symbol
                )
                for h in active_health(session)
            ),
            "OTHER_QUARANTINE_OR_PAUSE",
        )
        require(
            not any(
                i.kind == "CORE_EVIDENCE_CONFLICT"
                for i in repo.list_incidents(unresolved_only=True)
            ),
            "DISPUTED_SOURCE_EVIDENCE",
        )
        links = table("core_intent_links")
        owned = list(
            session.execute(
                select(links)
                .join(
                    table("order_intents"), links.c.intent_id == table("order_intents").c.intent_id
                )
                .where(
                    links.c.role == "ENTRY",
                    table("order_intents").c.symbol == symbol,
                    links.c.lot_id.not_in(select(table("core_round_trips").c.id)),
                )
            ).mappings()
        )
        require(len(owned) == 1, "SINGLE_OWNED_LOT_REQUIRED")
        link = owned[0]
        require(
            not any(
                h.id != target.id
                and h.scope == "LOT"
                and h.scope_id == link["lot_id"]
                and h.state in {"INTEGRITY_FAILURE", "MANUAL_PAUSE"}
                for h in active_health(session)
            ),
            "OTHER_QUARANTINE_OR_PAUSE",
        )
        intent = repo.get_order_intent(link["intent_id"])
        order = repo.get_broker_order_by_client_order_id(intent.client_order_id) if intent else None
        plan = load("core_protection_plans", link["protection_id"], ProtectionPlan)
        require(
            intent is not None
            and order is not None
            and intent.state is IntentState.FILLED
            and intent.strategy_id == plan.owner == store.config.owner
            and plan.generation == store.config.generation
            and plan.decision_id == link["decision_id"]
            and intent.side is OrderSide.BUY
            and intent.quantity == order.filled_quantity == delta[symbol]
            and (order.symbol, order.side, order.quantity, order.order_type, order.time_in_force)
            == (
                intent.symbol,
                intent.side,
                intent.quantity,
                intent.order_type,
                intent.time_in_force,
            ),
            "EXACT_OWNED_BUY_REQUIRED",
        )
        assert intent is not None and order is not None
        fills = repo.list_fills(broker_order_id=order.broker_order_id)
        missing_ids = set(proof.fill_ids) - set(failed.fill_ids)
        require(
            bool(fills)
            and missing_ids == {f.fill_id for f in fills}
            and sum((f.quantity for f in fills), Decimal(0)) == delta[symbol]
            and all(
                f.symbol == symbol
                and f.side is OrderSide.BUY
                and f.occurred_at <= failed.recorded_at_utc
                for f in fills
            )
            and sum((f.quantity * f.price for f in fills), Decimal(0)) / delta[symbol]
            == old_basis[symbol],
            "CANONICAL_FILLS_DO_NOT_EXPLAIN_FAILURE",
        )
        sources: list[str] = []
        for fill in fills:
            candidates = session.execute(
                select(table("core_broker_event_sources")).where(
                    func.json_extract(
                        table("core_broker_event_sources").c.payload, "$.fill.fill_id"
                    )
                    == fill.fill_id
                )
            ).mappings()
            matching = []
            for row in candidates:
                event = BrokerEvent.model_validate_json(row["payload"])
                if (
                    _source_id(event) == row["id"]
                    and event.fill == fill
                    and event.client_order_id == intent.client_order_id
                    and event.account_id == store.config.account_id
                    and event.evidence_lane == store.config.evidence_lane
                    and event.release_id == store.config.release_id
                    and event.provider == "alpaca"
                    and event.endpoint == "https://paper-api.alpaca.markets/v2/account/activities"
                ):
                    matching.append((row["id"], event))
            require(
                bool(matching)
                and min(e.recorded_at_utc for _, e in matching) > failed.recorded_at_utc,
                "LATE_CANONICAL_SOURCE_REQUIRED",
            )
            sources.extend(i for i, _ in matching)
        acknowledgements = []
        acquisitions = table("core_provider_acquisitions")
        ack_candidates = select(acquisitions).where(
            func.json_array_length(acquisitions.c.payload, "$.pending_owned_orders") > 0,
            func.json_extract(acquisitions.c.payload, "$.account_id") == store.config.account_id,
            func.json_extract(acquisitions.c.payload, "$.until")
            >= (failed.recorded_at_utc - timedelta(seconds=6)).isoformat(),
            func.json_extract(acquisitions.c.payload, "$.until")
            <= failed.recorded_at_utc.isoformat(),
        )
        for identity, encoded in session.execute(ack_candidates):
            document = json.loads(encoded)
            if not document.get("pending_owned_orders"):
                continue
            until = datetime.fromisoformat(document["until"])
            if (
                content_id(document) == identity
                and document.get("account_id") == store.config.account_id
                and document.get("release_id") == store.config.release_id
                and document.get("endpoint") == "https://paper-api.alpaca.markets"
                and 0 <= (failed.recorded_at_utc - until).total_seconds() <= 5
                and any(
                    BrokerOrder.model_validate(o) == order for o in document["pending_owned_orders"]
                )
            ):
                acknowledgements.append(identity)
        require(bool(acknowledgements), "CONTEMPORANEOUS_OWNED_ACK_REQUIRED")
        heartbeat_payload = session.execute(
            select(table("core_role_heartbeats").c.payload)
            .join(
                table("core_role_current"),
                table("core_role_current").c.heartbeat_id == table("core_role_heartbeats").c.id,
            )
            .where(table("core_role_current").c.role == "guardian")
        ).scalar_one_or_none()
        heartbeat = (
            RoleHeartbeat.model_validate_json(heartbeat_payload) if heartbeat_payload else None
        )
        require(
            heartbeat is not None
            and heartbeat.release_id == store.runtime_release_id
            and heartbeat.config_id == store.config_id
            and heartbeat.store_identity == store.database.store_identity
            and heartbeat.owner == store.config.owner
            and heartbeat.generation == store.config.generation
            and heartbeat.recorded_at <= now < heartbeat.valid_until
            and 0 <= (now - heartbeat.recorded_at).total_seconds() <= 30
            and all(r == "OWNED_PROTECTION_QUARANTINED:" + target.id for r in heartbeat.objections),
            "FRESH_BOUND_GUARDIAN_REQUIRED",
        )
        assert heartbeat is not None
        times = [
            load("core_time_evidence", i, TimeEvidence)
            for i in heartbeat.evidence_ids
            if session.execute(
                select(table("core_time_evidence").c.id).where(
                    table("core_time_evidence").c.id == i
                )
            ).first()
        ]
        require(len(times) == 1, "FRESH_TIME_REQUIRED")
        time = times[0]
        require(
            time.process_id == heartbeat.process_epoch
            and not time.objections
            and bool(time.samples)
            and 0 <= (now - time.wall_anchor).total_seconds() <= 30
            and time.samples[-1].provider.account_id == store.config.account_id
            and time.offset_upper - time.offset_lower <= 2,
            "FRESH_TIME_REQUIRED",
        )
        receipt = dict(
            contract="OWNED_BUY_QUARANTINE_RECOVERY_V1",
            at=now,
            approval=approval,
            health_id=target.id,
            failed_proof_id=failed.id,
            proof_id=proof.id,
            lot_id=link["lot_id"],
            original_client_order_id=intent.client_order_id,
            protection_id=plan.id,
            fill_source_ids=tuple(sorted(sources)),
            acknowledgement_ids=tuple(sorted(acknowledgements)),
            guardian_heartbeat_id=heartbeat.id,
            time_evidence_id=time.id,
        )
        receipt_id = content_id(receipt)
        put(session, "core_provider_acquisitions", receipt_id, receipt)
        recovered = Health(
            recorded_at_utc=now,
            evidence_lane=store.config.evidence_lane,
            release_id=store.config.release_id,
            role="GUARDIAN",
            scope="SYMBOL",
            scope_id=symbol,
            state="READY",
            objections=(),
            first_blocker=None,
            first_seen=now,
            last_seen=now,
            last_useful_progress=now,
            new_proof_id=proof.id,
            recovered_from=(target.id,),
            evidence_ids=(receipt_id, failed.id, proof.id),
            sequence=(
                session.execute(
                    select(
                        func.max(
                            func.json_extract(table("core_health_events").c.payload, "$.sequence")
                        )
                    )
                ).scalar_one()
                or 0
            )
            + 1,
        )
        # All historical health, order, fill, model and manual-stop records remain intact.
        put(session, "core_health_events", recovered.id, recovered)
        return receipt_id
