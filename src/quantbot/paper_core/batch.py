"""Deterministic prefixes and single-transaction claims for the bounded portfolio profile."""

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from quantbot.domain import OrderIntent, OrderSide, OrderType, TimeInForce
from quantbot.paper_core.activation import active_in_session, decision_model_objections
from quantbot.paper_core.authority import guardian_objections, runtime_objections
from quantbot.paper_core.contracts import (
    BatchPlan,
    Decision,
    EntryIntent,
    EntryReservation,
    ModelArtifact,
    ObservationCohort,
    ProtectionPlan,
    ReconciliationProof,
)
from quantbot.paper_core.entry_policy import (
    decision_entry_authority,
    entry_policy_in_session,
    entry_risk_allowed,
)
from quantbot.paper_core.health import active_health, entry_health_objections
from quantbot.paper_core.portfolio import PortfolioBudget, portfolio_budget
from quantbot.paper_core.selection import (
    boundary_id,
    cohort_document,
    cohort_symbols,
    evaluate_cohort,
    reservation_state,
    validate_cohort,
)
from quantbot.paper_core.store import CoreStore, latest_certification, put, table
from quantbot.paper_core.time_evidence import (
    TimeEvidence,
    entry_time_objections,
    evidence_objections,
    project_time_evidence,
)
from quantbot.paper_core.work import current_receipts, dirty
from quantbot.research.manifest import canonical_result_json, content_id
from quantbot.storage import StorageRepository
from quantbot.storage.database import encode_utc


def _existing_batch(session: Session, boundary: str) -> BatchPlan | None:
    index, documents = table("core_document_index"), table("core_batches")
    raw = session.execute(
        select(documents.c.payload)
        .join(index, index.c.id == documents.c.id)
        .where(index.c.kind == "core_batch_boundary", index.c.reference == boundary)
    ).scalar_one_or_none()
    return BatchPlan.model_validate_json(raw) if raw else None


def _decisions(session: Session, batch: BatchPlan) -> tuple[Decision, ...]:
    documents = table("core_decisions")
    return tuple(
        Decision.model_validate_json(
            session.execute(
                select(documents.c.payload).where(documents.c.id == identity)
            ).scalar_one()
        )
        for identity in batch.decision_ids
    )


def _prefix(
    store: CoreStore,
    session: Session,
    cohort: ObservationCohort,
    model: ModelArtifact,
    proof: ReconciliationProof,
    budget: PortfolioBudget,
    *,
    authority: dict[str, Any],
) -> tuple[Decision, ...]:
    policy = entry_policy_in_session(store, session)
    validate_cohort(store.config, cohort, entry_policy=policy)
    if not cohort.observations:
        return ()
    daily, prior, _ = reservation_state(store, session, cohort)
    events = active_health(session)
    health = {
        s: entry_health_objections(store, session, s, health_events=events)
        for s in cohort_symbols(store.config, cohort, policy)
    }
    for symbol in budget.occupied_symbols:
        health[symbol] = tuple(dict.fromkeys((*health.get(symbol, ()), "SYMBOL_ALREADY_RESERVED")))
    slots = budget.remaining_slots
    left_cash, left_gross, left_risk = (
        budget.spendable_cash,
        budget.gross_headroom,
        budget.risk_headroom,
    )
    chosen: list[Decision] = []
    for _ in range(max(1, min(slots, len(cohort.observations)))):
        reasons = budget.objections or (() if slots else ("PORTFOLIO_SYMBOL_CAPACITY",))
        context = {
            s: tuple(dict.fromkeys((*health.get(s, ()), *reasons)))
            for s in cohort_symbols(store.config, cohort, policy)
        }
        decision = evaluate_cohort(
            store.config,
            cohort,
            model,
            proof,
            session_counts=daily,
            prior_counts=prior,
            health_reasons=context,
            entry_policy=policy,
            available_notional=min(left_cash, left_gross, left_risk * 100),
            **authority,
        )
        if decision.action != "BUY":
            return tuple(chosen) if chosen else (decision,)
        chosen.append(decision)
        health[decision.symbol] = ("BATCH_MEMBER_ALREADY_SELECTED",)
        notional = decision.quantity * decision.price
        left_cash -= notional
        left_gross -= notional
        left_risk -= notional * Decimal(".01")
    return tuple(chosen)


def evaluate_batch(
    store: CoreStore,
    cohort: ObservationCohort,
    model: ModelArtifact,
    proof: ReconciliationProof,
    *,
    now: datetime,
) -> tuple[Decision, ...]:
    if not store.parallel_entries:
        raise ValueError("batch requires the resource-bounded PAPER admission policy")
    with store.database.transaction() as session:
        # A Guardian refresh can supersede the acquisition passed by Trader. Read
        # certification, ledger and budget from one SQLite snapshot, then retain
        # the normal current-authority recheck under claim_batch's write lock.
        session.execute(text("BEGIN"))
        existing = _existing_batch(session, boundary_id(store.config, cohort))
        if existing:
            return _decisions(session, existing)
        certification = latest_certification(session)
        if certification is not None:
            proofs = table("core_reconciliation_proofs")
            proof = ReconciliationProof.model_validate_json(
                session.execute(
                    select(proofs.c.payload).where(proofs.c.id == certification[1])
                ).scalar_one()
            )
        active = active_in_session(store, session)
        timing = (
            store.read("core_time_evidence", store.runtime_time_evidence_id, TimeEvidence)
            if store.runtime_time_evidence_id
            else None
        )
        authority = dict(
            time_evidence=timing,
            time_projection=project_time_evidence(timing, store.clocks) if timing else None,
            activation=active[1] if active else None,
        )
        budget = portfolio_budget(store, session, proof, now=max(now, store.clocks.sample()[0]))
        return _prefix(store, session, cohort, model, proof, budget, authority=authority)


_CLAIM_REFUSAL_STAGES = frozenset(
    {
        "CANDIDATE_NOT_BUY",
        "ENTRY_DISABLED",
        "DECISION_INTERVAL",
        "MODEL_AUTHORITY",
        "RUNTIME_AUTHORITY",
        "GUARDIAN_READINESS",
        "GUARDIAN_RECEIPT",
        "CERTIFICATION_MISSING",
        "CURRENT_BUDGET_OR_LEDGER",
        "TIME_EVIDENCE",
        "BATCH_CAPACITY",
        "BATCH_NOTIONAL",
        "MEMBER_AUTHORITY",
    }
)


def _emit_claim_refusal(
    store: CoreStore,
    decisions: tuple[Decision, ...],
    stage: str,
    *,
    requested_at: datetime,
    elapsed_seconds: float,
) -> None:
    """Fixed-size negative diagnostic; never an execution or freshness attestation."""
    if stage not in _CLAIM_REFUSAL_STAGES or not decisions:
        return
    release = store.runtime_release_id
    print(
        json.dumps(
            {
                "event": "PAPER_BATCH_CLAIM_REFUSED",
                "stage": stage,
                "first_decision_id": decisions[0].id,
                "decision_count": len(decisions),
                "executable_release_id": release
                if release and re.fullmatch(r"[0-9a-f]{64}", release)
                else None,
                "requested_at_utc": requested_at.isoformat(),
                "emitted_after_transaction_at_utc": datetime.now(UTC).isoformat(),
                "elapsed_seconds": max(0.0, elapsed_seconds),
            },
            separators=(",", ":"),
        ),
        flush=True,
    )


def claim_batch(
    store: CoreStore, decisions: tuple[Decision, ...], *, now: datetime
) -> tuple[EntryIntent, ...]:
    refused: list[str] = []
    started = monotonic()
    result = _claim_batch(store, decisions, now=now, refused=refused)
    # The inner context has committed and released its writer before formatting or
    # output. Exceptions/commit failures never pass this point or masquerade as a
    # guard refusal. Diagnostic failure cannot change the authority result.
    if refused:
        try:
            _emit_claim_refusal(
                store,
                decisions,
                refused[0],
                requested_at=now,
                elapsed_seconds=monotonic() - started,
            )
        except Exception:
            pass
    return result


def _claim_batch(
    store: CoreStore, decisions: tuple[Decision, ...], *, now: datetime, refused: list[str]
) -> tuple[EntryIntent, ...]:
    def decline(stage: str) -> tuple[EntryIntent, ...]:
        refused.append(stage)
        return ()

    if not decisions:
        return ()
    if not store.parallel_entries:
        raise ValueError("batch requires the resource-bounded PAPER admission policy")
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        for decision in decisions:
            put(session, "core_decisions", decision.id, decision)
        first = decisions[0]
        if first.selection is None:
            raise ValueError("batch requires captured selection authority")
        cohort = first.selection.cohort
        boundary = boundary_id(store.config, cohort)
        existing = _existing_batch(session, boundary)
        links = table("core_intent_links")
        if existing:
            wrappers = {
                r["decision_id"]: EntryIntent.model_validate_json(r["payload"])
                for r in session.execute(
                    select(links).where(
                        links.c.role == "ENTRY", links.c.decision_id.in_(existing.decision_ids)
                    )
                ).mappings()
            }
            return tuple(wrappers[i] for i in existing.decision_ids)
        if not all(d.action == "BUY" for d in decisions):
            return decline("CANDIDATE_NOT_BUY")
        if len(decisions) > len(cohort.observations) or len({d.symbol for d in decisions}) != len(
            decisions
        ):
            raise ValueError("batch members must be distinct and bounded")
        if not store.runtime_entry_enabled:
            return decline("ENTRY_DISABLED")
        actual_now = store.clocks.sample()[0]
        if not first.recorded_at_utc <= actual_now < first.valid_until:
            return decline("DECISION_INTERVAL")
        document = cohort_document(store, cohort)
        raw = session.execute(
            select(table("core_provider_acquisitions").c.payload).where(
                table("core_provider_acquisitions").c.id == content_id(document)
            )
        ).scalar_one_or_none()
        if raw != canonical_result_json(document):
            raise ValueError("batch does not match a captured cohort")
        active = active_in_session(store, session)
        if active is None or any(decision_model_objections(store, session, d) for d in decisions):
            return decline("MODEL_AUTHORITY")
        if store.config.evidence_lane == "BROKER_PAPER" and runtime_objections(
            store, session, entry=True
        ):
            return decline("RUNTIME_AUTHORITY")
        if guardian_objections(store, session, now=actual_now):
            return decline("GUARDIAN_READINESS")
        receipt = current_receipts(session)
        if not any(
            r.owner == store.config.owner
            and r.generation == store.config.generation
            and r.recorded_at_utc <= actual_now < r.valid_until
            for r in receipt
        ):
            return decline("GUARDIAN_RECEIPT")
        certification = latest_certification(session)
        if certification is None:
            return decline("CERTIFICATION_MISSING")
        proofs = table("core_reconciliation_proofs")
        current = ReconciliationProof.model_validate_json(
            session.execute(
                select(proofs.c.payload).where(proofs.c.id == certification[1])
            ).scalar_one()
        )
        original = ReconciliationProof.model_validate_json(
            session.execute(
                select(proofs.c.payload).where(proofs.c.id == first.inventory_id)
            ).scalar_one()
        )
        current_budget = portfolio_budget(store, session, current, now=actual_now)
        if current_budget.objections or original.ledger_state_id != current.ledger_state_id:
            return decline("CURRENT_BUDGET_OR_LEDGER")
        timing = (
            store.read("core_time_evidence", first.time_evidence_id, TimeEvidence)
            if first.time_evidence_id
            else None
        )
        if timing is None or evidence_objections(timing, store.clocks):
            return decline("TIME_EVIDENCE")
        replay_budget = portfolio_budget(
            store, session, original, now=first.recorded_at_utc, latest=False
        )
        expected = _prefix(
            store,
            session,
            cohort,
            active[0],
            original,
            replay_budget,
            authority=dict(
                time_evidence=timing, time_projection=first.time_projection, activation=active[1]
            ),
        )
        if decisions != expected:
            raise ValueError("batch does not match its certified deterministic prefix")
        if len(decisions) > current_budget.remaining_slots:
            return decline("BATCH_CAPACITY")
        total = sum((d.quantity * d.price for d in decisions), Decimal(0))
        if total > current_budget.available_notional:
            return decline("BATCH_NOTIONAL")
        for d in decisions:
            observation = next(o for o in cohort.observations if o.id == d.observation_id)
            policy_reasons, minimum_share = decision_entry_authority(
                store, session, d, now=actual_now
            )
            if (
                d.config_id != store.config_id
                or d.selection is None
                or d.selection.cohort != cohort
                or d.recorded_at_utc != first.recorded_at_utc
                or d.inventory_id != first.inventory_id
                or d.time_evidence_id != first.time_evidence_id
                or entry_time_objections(
                    observation,
                    timing,
                    store.config,
                    projection=project_time_evidence(timing, store.clocks),
                )
                or policy_reasons
                or not entry_risk_allowed(
                    current.account,
                    d.quantity,
                    d.price,
                    config=store.config,
                    policy=entry_policy_in_session(store, session),
                    available_notional=current_budget.available_notional,
                    minimum_whole_share=minimum_share,
                    eligibility=observation.asset_eligibility,
                    at=store.clocks.sample()[0],
                )
                or dict(current.actual_quantities).get(d.symbol, Decimal(0)) != 0
                or session.execute(
                    select(links.c.intent_id).where(
                        links.c.role == "ENTRY", links.c.observation_id == d.observation_id
                    )
                ).first()
            ):
                return decline("MEMBER_AUTHORITY")
            stored = session.execute(
                select(table("core_observations").c.payload).where(
                    table("core_observations").c.id == observation.id
                )
            ).scalar_one_or_none()
            if stored != canonical_result_json(observation):
                raise ValueError("batch observation is not the captured immutable member")
        selected = {d.symbol for d in decisions}
        abstentions = tuple((s, reason) for s, reason in cohort.unavailable) + tuple(
            (o.symbol, "BATCH_CAPACITY_OR_ELIGIBILITY")
            for o in cohort.observations
            if o.symbol not in selected
        )
        batch = BatchPlan(
            recorded_at_utc=first.recorded_at_utc,
            evidence_lane=store.config.evidence_lane,
            release_id=store.config.release_id,
            source_refs=(content_id(document), current.id, active[0].id),
            account_id=store.config.account_id,
            store_identity=store.database.store_identity,
            config_id=store.config_id,
            policy_id=store.config.policy_id,
            session_date=cohort.session_open.date(),
            boundary_id=boundary,
            cohort_id=content_id(document),
            model_id=active[0].id,
            proof_id=original.id,
            opening_id=current_budget.opening_id,
            decision_ids=tuple(d.id for d in decisions),
            abstentions=abstentions,
        )
        put(session, "core_batches", batch.id, batch)
        session.execute(
            table("core_document_index")
            .insert()
            .values(
                kind="core_batch_boundary",
                id=batch.id,
                reference=boundary,
                available_at=encode_utc(first.recorded_at_utc),
            )
        )
        repo = StorageRepository(session)
        result = []
        for d in decisions:
            plan = ProtectionPlan.for_decision(d, store.config)
            digest = content_id({"boundary": boundary, "decision": d.id})
            intent = OrderIntent(
                intent_id="core-entry-" + digest,
                client_order_id="qb-" + digest[:40],
                strategy_id=store.config.owner,
                signal_date=cohort.session_open.date(),
                symbol=d.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                time_in_force=(
                    TimeInForce.GTC if d.market_calendar == "CRYPTO24" else TimeInForce.DAY
                ),
                quantity=d.quantity,
                created_at=d.recorded_at_utc,
            )
            reservation = EntryReservation(
                recorded_at_utc=d.recorded_at_utc,
                evidence_lane=store.config.evidence_lane,
                release_id=store.config.release_id,
                account_id=store.config.account_id,
                store_identity=store.database.store_identity,
                config_id=store.config_id,
                policy_id=store.config.policy_id,
                session_date=cohort.session_open.date(),
                batch_id=batch.id,
                decision_id=d.id,
                intent_id=intent.intent_id,
                client_order_id=intent.client_order_id,
                symbol=d.symbol,
                quantity=d.quantity,
                reserved_notional=d.quantity * d.price,
                stop_risk=d.quantity * (d.price - plan.stop_price),
            )
            put(session, "core_protection_plans", plan.id, plan)
            put(session, "core_entry_reservations", reservation.id, reservation)
            repo.create_order_intent(intent)
            wrapper = EntryIntent(intent=intent, decision_id=d.id, protection_id=plan.id)
            session.execute(
                links.insert().values(
                    intent_id=intent.intent_id,
                    decision_id=d.id,
                    protection_id=plan.id,
                    observation_id=d.observation_id,
                    role="ENTRY",
                    lot_id=d.id,
                    payload=canonical_result_json(wrapper),
                )
            )
            session.execute(
                table("core_batch_members")
                .insert()
                .values(
                    batch_id=batch.id,
                    symbol=d.symbol,
                    decision_id=d.id,
                    reservation_id=reservation.id,
                    session_date=cohort.session_open.date().isoformat(),
                )
            )
            dirty(session, (d.id,), available_at=d.recorded_at_utc, _initial_claim=True)
            result.append(wrapper)
        return tuple(result)


def member_objections(
    store: CoreStore, session: Session, decision: Decision, intent_id: str | None
) -> tuple[str, ...]:
    """A sibling may share a boundary only through its exact committed reservation."""
    members = table("core_batch_members")
    row = (
        session.execute(select(members).where(members.c.decision_id == decision.id))
        .mappings()
        .one_or_none()
    )
    if row is None or intent_id is None:
        return ("BATCH_RESERVATION_AUTHORITY_MISSING",)
    reservation_raw = session.execute(
        select(table("core_entry_reservations").c.payload).where(
            table("core_entry_reservations").c.id == row["reservation_id"]
        )
    ).scalar_one_or_none()
    batch_raw = session.execute(
        select(table("core_batches").c.payload).where(table("core_batches").c.id == row["batch_id"])
    ).scalar_one_or_none()
    if reservation_raw is None or batch_raw is None or decision.selection is None:
        return ("BATCH_RESERVATION_AUTHORITY_MISSING",)
    reservation = EntryReservation.model_validate_json(reservation_raw)
    batch = BatchPlan.model_validate_json(batch_raw)
    intent = StorageRepository(session).get_order_intent(intent_id)
    link = (
        session.execute(
            select(table("core_intent_links")).where(
                table("core_intent_links").c.intent_id == intent_id,
                table("core_intent_links").c.role == "ENTRY",
            )
        )
        .mappings()
        .one_or_none()
    )
    plan = ProtectionPlan.for_decision(decision, store.config)
    boundary = boundary_id(store.config, decision.selection.cohort)
    original = _existing_batch(session, boundary)
    if (
        intent is None
        or link is None
        or original is None
        or original.id != batch.id
        or batch.id != row["batch_id"]
        or reservation.id != row["reservation_id"]
        or batch.account_id != store.config.account_id
        or batch.store_identity != store.database.store_identity
        or batch.config_id != store.config_id
        or batch.policy_id != store.config.policy_id
        or batch.evidence_lane != store.config.evidence_lane
        or batch.release_id != store.config.release_id
        or batch.boundary_id != boundary
        or batch.proof_id != decision.inventory_id
        or batch.model_id != decision.model_id
        or decision.id not in batch.decision_ids
        or reservation.batch_id != batch.id
        or reservation.decision_id != decision.id
        or reservation.account_id != store.config.account_id
        or reservation.store_identity != store.database.store_identity
        or reservation.config_id != store.config_id
        or reservation.policy_id != store.config.policy_id
        or reservation.evidence_lane != store.config.evidence_lane
        or reservation.release_id != store.config.release_id
        or reservation.intent_id != intent_id
        or reservation.client_order_id != intent.client_order_id
        or reservation.symbol != decision.symbol
        or row["symbol"] != decision.symbol
        or reservation.quantity != decision.quantity
        or intent.quantity != decision.quantity
        or intent.strategy_id != store.config.owner
        or intent.symbol != decision.symbol
        or intent.side is not OrderSide.BUY
        or intent.order_type is not OrderType.MARKET
        or intent.time_in_force
        != (TimeInForce.GTC if decision.market_calendar == "CRYPTO24" else TimeInForce.DAY)
        or reservation.reserved_notional != decision.quantity * decision.price
        or reservation.stop_risk != decision.quantity * (decision.price - plan.stop_price)
        or link["lot_id"] != decision.id
        or link["decision_id"] != decision.id
        or link["protection_id"] != plan.id
        or link["observation_id"] != decision.observation_id
    ):
        return ("BATCH_RESERVATION_AUTHORITY_MISMATCH",)
    return ()


def compatible_batch_successor(
    store: CoreStore,
    session: Session,
    decision: Decision,
    current: ReconciliationProof,
    intent: OrderIntent | None,
    *,
    now: datetime,
) -> bool:
    if intent is None or member_objections(store, session, decision, intent.intent_id):
        return False
    raw = session.execute(
        select(table("core_reconciliation_proofs").c.payload).where(
            table("core_reconciliation_proofs").c.id == decision.inventory_id
        )
    ).scalar_one_or_none()
    if raw is None:
        return False
    original = ReconciliationProof.model_validate_json(raw)
    certifications = table(
        "core_inventory_certifications"
        if original.cash_complete
        else "core_quantity_certifications"
    )
    generation = session.execute(
        select(certifications.c.generation).where(certifications.c.proof_id == original.id)
    ).scalar_one_or_none()
    if (
        generation != original.generation
        or generation != decision.inventory_generation
        or original.baseline_id != current.baseline_id
        or original.account.account_id != current.account.account_id
        or current.account.account_id != store.config.account_id
        or original.release_id != current.release_id
        or current.release_id != store.config.release_id
        or original.evidence_lane != current.evidence_lane
        or current.evidence_lane != store.config.evidence_lane
        or original.generation > current.generation
        or not original.recorded_at_utc <= current.recorded_at_utc <= now
    ):
        return False
    budget = portfolio_budget(store, session, current, now=now, exclude_intent=intent.intent_id)
    _, minimum_share = decision_entry_authority(store, session, decision, now=now)
    return (
        not budget.objections
        and entry_risk_allowed(
            current.account,
            intent.quantity,
            decision.price,
            config=store.config,
            policy=entry_policy_in_session(store, session),
            available_notional=budget.available_notional,
            minimum_whole_share=minimum_share,
            at=now,
        )
        and intent.quantity
        * (decision.price - ProtectionPlan.for_decision(decision, store.config).stop_price)
        <= budget.risk_headroom
    )
