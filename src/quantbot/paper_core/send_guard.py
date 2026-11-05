"""Durable entry lifetime consumed at the coordinator's actual send boundary."""

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from quantbot.domain import IntentState, OrderIntent
from quantbot.paper_core.contracts import (
    Decision,
    EntryIntent,
    ProtectionPlan,
    ReconciliationProof,
)
from quantbot.paper_core.entry_policy import (
    AssetEligibility,
    decision_entry_authority,
    entry_risk_allowed,
)
from quantbot.paper_core.funds import entry_inventory_objections
from quantbot.paper_core.health import entry_health_objections
from quantbot.paper_core.portfolio import CurrentPortfolioSnapshot
from quantbot.paper_core.settlement import (
    EXIT_REFUSAL as EXIT_REFUSAL,
)
from quantbot.paper_core.settlement import (
    REFUSAL as REFUSAL,
)
from quantbot.paper_core.settlement import (
    definitively_unsent as definitively_unsent,
)
from quantbot.paper_core.store import CoreStore, latest_certification, table
from quantbot.paper_core.time_evidence import TimeEvidence, evidence_objections, provider_interval
from quantbot.research.manifest import content_id
from quantbot.risk import OrderPurpose
from quantbot.storage import StorageRepository


def record_pre_send_refusal(
    session: Session,
    intent: OrderIntent,
    purpose: OrderPurpose,
    reasons: tuple[str, ...],
    *,
    clock: Callable[[], datetime],
    evidence_ids: tuple[str, ...] = (),
    source: str = "coordinator-pre-post-gate",
    evaluation_scope: str | None = None,
) -> None:
    """Latch a known pre-POST refusal in the same transaction as its ERROR state."""
    if purpose not in {OrderPurpose.ENTRY, OrderPurpose.STRATEGY_EXIT}:
        return
    refusal = REFUSAL if purpose is OrderPurpose.ENTRY else EXIT_REFUSAL
    repo = StorageRepository(session)
    stored = repo.get_order_intent(intent.intent_id)
    if stored is None or stored.state is not IntentState.ORDER_CREATED:
        raise ValueError("pre-send refusal requires ORDER_CREATED")
    if any(e.event_type == refusal for e in repo.list_order_events(intent_id=intent.intent_id)):
        return
    repo.record_order_event(
        "core-send-refused-" + content_id({"intent": intent.intent_id}),
        event_type=refusal,
        occurred_at=clock(),
        intent_id=intent.intent_id,
        detail={
            "guard_contract": "core-final-send-v1",
            "reasons": tuple(dict.fromkeys(reasons)),
            "definitively_unsent": repo.get_broker_order_by_client_order_id(intent.client_order_id)
            is None,
            "state_at_refusal": stored.state.value,
            "source": source,
            "purpose": purpose.value,
            **({"evidence_ids": evidence_ids} if evidence_ids else {}),
            **({"evaluation_scope": evaluation_scope} if evaluation_scope is not None else {}),
        },
    )


def compatible_successor(
    store: CoreStore,
    session: Session,
    decision: Decision,
    current: ReconciliationProof,
    intent: OrderIntent | None,
    *,
    now: datetime,
) -> bool:
    """Accept a certified refresh, never a different economic ledger or risk envelope."""
    if entry_inventory_objections(store, session, current, now=now):
        return False

    if store.parallel_entries:
        from quantbot.paper_core.batch import compatible_batch_successor

        return compatible_batch_successor(store, session, decision, current, intent, now=now)
    if any(
        intent is None or pending.intent_id != intent.intent_id
        for pending in StorageRepository(session).list_unresolved_order_intents()
    ):
        return False
    if decision.inventory_id == current.id:
        return True
    if store.runtime_role is None and store.config.evidence_lane != "BROKER_PAPER":
        return False
    encoded = session.execute(
        select(table("core_reconciliation_proofs").c.payload).where(
            table("core_reconciliation_proofs").c.id == decision.inventory_id
        )
    ).scalar_one_or_none()
    if encoded is None:
        return False
    original = ReconciliationProof.model_validate_json(encoded)
    certs = table(
        "core_inventory_certifications"
        if original.cash_complete
        else "core_quantity_certifications"
    )
    certified = session.execute(
        select(certs.c.generation).where(certs.c.proof_id == original.id)
    ).scalar_one_or_none()

    def basis(proof: ReconciliationProof) -> list[tuple[str, Decimal, Decimal]]:
        return sorted((p.symbol, p.quantity, p.average_entry_price) for p in proof.positions)

    return (
        original.id == decision.inventory_id
        and certified == original.generation == decision.inventory_generation
        and original.complete
        and not entry_inventory_objections(store, session, original, now=now, latest=False)
        and current.complete
        and original.ledger_state_id is not None
        and original.ledger_state_id == current.ledger_state_id == store.ledger_state_id(session)
        and original.generation < current.generation
        and original.recorded_at_utc <= current.recorded_at_utc <= now
        and (now - current.recorded_at_utc).total_seconds() <= 120
        and original.account.account_id == current.account.account_id == store.config.account_id
        and original.account.currency == current.account.currency
        and original.account.cash == current.account.cash
        and original.baseline_id == current.baseline_id
        and original.release_id == current.release_id == store.config.release_id
        and original.evidence_lane == current.evidence_lane == store.config.evidence_lane
        and original.fill_ids == current.fill_ids
        and original.order_ids == current.order_ids
        and original.expected_quantities == current.expected_quantities
        and original.actual_quantities == current.actual_quantities
        and original.expected_cash == current.expected_cash
        and basis(original) == basis(current)
        and not session.execute(
            select(table("core_cash_authority_revocations").c.snapshot_id).where(
                table("core_cash_authority_revocations").c.snapshot_id
                == "core-certified-" + original.id
            )
        ).first()
    )


def current_risk_allowed(
    proof: ReconciliationProof,
    *,
    quantity: Decimal,
    price: Decimal,
    minimum_whole_share: bool = False,
) -> bool:
    return entry_risk_allowed(
        proof.account, quantity, price, minimum_whole_share=minimum_whole_share
    )


def final_send_objections(
    store: CoreStore, intent: OrderIntent, purpose: OrderPurpose, *, clock: Callable[[], datetime]
) -> tuple[str, ...]:
    if purpose is not OrderPurpose.ENTRY and store.config.evidence_lane != "BROKER_PAPER":
        return ()
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        return final_send_in_session(store, session, intent, purpose, clock=clock)


def final_send_in_session(
    store: CoreStore,
    session: Session,
    intent: OrderIntent,
    purpose: OrderPurpose,
    *,
    clock: Callable[[], datetime],
    asset_eligibility: Callable[[], AssetEligibility | None] | None = None,
    market_snapshot: Callable[[], CurrentPortfolioSnapshot | None] | None = None,
) -> tuple[str, ...]:
    """Validate authority using the coordinator's locked send-state transaction."""
    from quantbot.paper_core.authority import guardian_objections, runtime_objections

    runtime_reasons: tuple[str, ...] = ()
    if store.config.evidence_lane == "BROKER_PAPER":
        runtime_reasons = runtime_objections(store, session, entry=purpose is OrderPurpose.ENTRY)
        if not getattr(store, "runtime_submission_enabled", False):
            runtime_reasons = (*runtime_reasons, "CORE_SUBMISSION_DISABLED")
        if purpose is OrderPurpose.ENTRY and not store.runtime_entry_enabled:
            runtime_reasons = (*runtime_reasons, "CORE_ENTRY_DISABLED")
        if "CORE_RUNTIME_BINDING_MISMATCH" in runtime_reasons:
            # A fenced old writer cannot evaluate the successor's admission policy.
            # Preserve the explicit refusal instead of raising from a later policy read.
            return runtime_reasons
    if purpose is not OrderPurpose.ENTRY:
        if store.config.evidence_lane == "BROKER_PAPER":
            runtime_reasons = (*runtime_reasons, *owned_exit_objections(store, session, intent))
        return runtime_reasons
    from quantbot.paper_core.account_control import entry_lease_objections

    runtime_reasons = (*runtime_reasons, *entry_lease_objections(store))
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
    current_asset = asset_eligibility() if asset_eligibility else None
    policy_reasons, minimum_share = decision_entry_authority(
        store, session, decision, now=clock(), fresh=current_asset, require_fresh=True
    )
    if current_asset is not None:
        from quantbot.paper_core.store import put

        put(session, "core_provider_acquisitions", current_asset.id, current_asset)
    plan = ProtectionPlan.model_validate_json(
        session.execute(
            select(table("core_protection_plans").c.payload).where(
                table("core_protection_plans").c.id == link["protection_id"]
            )
        ).scalar_one()
    )
    from quantbot.paper_core.work import current_receipts

    receipts = current_receipts(session, plan_id=plan.id)
    receipts = [
        r
        for r in receipts
        if r.owner == plan.owner and r.generation == plan.generation and plan.id in r.protection_ids
    ]
    receipt = max(receipts, key=lambda r: (r.recorded_at_utc, r.id), default=None)
    # Sample after durable reads and writer-lock acquisition, never the caller's decision time.
    now = store.clocks.sample()[0] if store.config.clock_profile == "PAPER_TIME_V2" else clock()
    reasons: list[str] = list(runtime_reasons)
    from quantbot.paper_core.selection import reservation_objections

    reasons.extend(reservation_objections(store, session, decision, intent_id=intent.intent_id))
    reasons.extend(
        guardian_objections(store, session, now=now, receipt_id=receipt.id if receipt else None)
    )
    from quantbot.paper_core.activation import decision_model_objections

    reasons.extend(decision_model_objections(store, session, decision))
    if repo.get_kill_switch_state().engaged:
        reasons.append("CORE_MANUAL_ENTRY_PAUSE")
    reasons.extend(entry_health_objections(store, session, decision.symbol))
    if store.config.clock_profile == "PAPER_TIME_V2":
        if (
            plan.clock_process_id == store.clocks.process_id
            and plan.monotonic_expiry is not None
            and store.clocks.sample()[1] >= plan.monotonic_expiry
        ):
            reasons.append("CORE_PROTECTION_DEADLINE_DUE")
        payload = session.execute(
            select(table("core_time_evidence").c.payload).where(
                table("core_time_evidence").c.id == decision.time_evidence_id
            )
        ).scalar_one_or_none()
        if payload is None or plan.clock_profile != store.config.clock_profile:
            reasons.append("TIME_EVIDENCE_MISSING")
        else:
            evidence = TimeEvidence.model_validate_json(payload)
            reasons.extend(evidence_objections(evidence, store.clocks))
            earliest, latest_time = provider_interval(evidence, store.clocks)
            if latest_time >= decision.valid_until:
                reasons.append("TIME_OBSERVATION_BOUNDARY")
            if latest_time >= plan.deadline:
                reasons.append("CORE_PROTECTION_DEADLINE_DUE")
            if latest_time >= decision.session_close - timedelta(seconds=120):
                reasons.append("TIME_SESSION_BOUNDARY")
            if evidence.samples and earliest < evidence.samples[-1].provider.session_open:
                reasons.append("TIME_SESSION_BOUNDARY")
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
        or entry_inventory_objections(store, session, proof, now=now)
        or not compatible_successor(store, session, decision, proof, intent, now=now)
        or session.execute(
            select(table("core_cash_authority_revocations").c.snapshot_id).where(
                table("core_cash_authority_revocations").c.snapshot_id
                == "core-certified-" + proof.id
            )
        ).first()
    ):
        reasons.append("CORE_CASH_AUTHORITY_UNAVAILABLE")

    if proof is not None and store.parallel_entries:
        from quantbot.paper_core.portfolio import portfolio_budget
        from quantbot.paper_core.store import put

        mark = market_snapshot() if market_snapshot else None
        if mark is None and store.config.evidence_lane == "BROKER_PAPER":
            reasons.append("PORTFOLIO_CURRENT_MARKS_UNAVAILABLE")
        if mark is not None:
            put(session, "core_provider_acquisitions", mark.id, mark)
        budget = portfolio_budget(
            store, session, proof, now=now, exclude_intent=intent.intent_id, market_snapshot=mark
        )
        reasons.extend(budget.objections)
        from quantbot.paper_core.entry_policy import entry_policy_in_session
        from quantbot.paper_core.portfolio import current_entry_price

        quote_reasons, risk_price = current_entry_price(store, mark, intent, decision, now=now)
        reasons.extend(quote_reasons)
        risk_account = mark.account if mark is not None else proof.account
        if intent.quantity * (risk_price - plan.stop_price) > min(
            budget.risk_headroom, risk_account.equity / 400
        ) or not entry_risk_allowed(
            risk_account,
            intent.quantity,
            risk_price,
            config=store.config,
            policy=entry_policy_in_session(store, session),
            available_notional=budget.available_notional,
            minimum_whole_share=minimum_share,
        ):
            reasons.append("CORE_FIXED_RISK_ENVELOPE")
    elif proof is not None and (
        store.runtime_role is not None or store.config.evidence_lane == "BROKER_PAPER"
    ):
        if not current_risk_allowed(
            proof, quantity=intent.quantity, price=decision.price, minimum_whole_share=minimum_share
        ):
            reasons.append("CORE_FIXED_RISK_ENVELOPE")
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
    reasons.extend(policy_reasons)
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
                **(
                    {
                        "entry_policy_id": decision.entry_policy_id,
                        "evidence_ids": (current_asset.id, decision.entry_policy_id),
                    }
                    if current_asset is not None and decision.entry_policy_id
                    else {}
                ),
            },
        )
    elif store.config.evidence_lane == "BROKER_PAPER":
        repo.record_order_event(
            "core-send-authorized-" + content_id({"intent": intent.intent_id}),
            event_type="CORE_LOCKED_SEND_AUTHORIZED",
            occurred_at=now,
            intent_id=intent.intent_id,
            detail={
                "decision_id": decision.id,
                "protection_id": plan.id,
                "guardian_receipt_id": receipt.id if receipt else None,
                "config_id": store.config_id,
                "process_epoch": store.clocks.process_id,
                "contract": "core-final-send-v1",
                **(
                    {
                        "entry_policy_id": decision.entry_policy_id,
                        "asset_eligibility_id": current_asset.id if current_asset else None,
                    }
                    if decision.entry_policy_id
                    else {}
                ),
            },
        )
    return tuple(reasons)


def owned_exit_objections(
    store: CoreStore, session: Session, intent: OrderIntent
) -> tuple[str, ...]:
    from decimal import Decimal

    from quantbot.domain import OrderSide

    repo = StorageRepository(session)
    links = table("core_intent_links")
    link = (
        session.execute(
            select(links).where(links.c.intent_id == intent.intent_id, links.c.role == "EXIT")
        )
        .mappings()
        .one_or_none()
    )
    if link is None:
        return ("CORE_EXIT_HANDOFF_MISSING",)
    plan = store.read("core_protection_plans", link["protection_id"], ProtectionPlan)
    entry = (
        session.execute(
            select(links).where(links.c.lot_id == link["lot_id"], links.c.role == "ENTRY")
        )
        .mappings()
        .one_or_none()
    )
    if entry is None or intent.strategy_id != plan.owner or intent.side is not OrderSide.SELL:
        return ("CORE_EXIT_OWNERSHIP_MISMATCH",)
    original = repo.get_order_intent(entry["intent_id"])
    if original is None or original.strategy_id != plan.owner or original.symbol != intent.symbol:
        return ("CORE_EXIT_OWNERSHIP_MISMATCH",)
    entry_order = repo.get_broker_order_by_client_order_id(original.client_order_id)
    owned = (
        Decimal(0)
        if entry_order is None
        else sum(
            (f.quantity for f in repo.list_fills(broker_order_id=entry_order.broker_order_id)),
            Decimal(0),
        )
    )
    for exit_link in session.execute(
        select(links).where(links.c.lot_id == link["lot_id"], links.c.role == "EXIT")
    ).mappings():
        exit_intent = repo.get_order_intent(exit_link["intent_id"])
        if exit_intent is None:
            return ("CORE_EXIT_OWNERSHIP_MISMATCH",)
        order = repo.get_broker_order_by_client_order_id(exit_intent.client_order_id)
        filled = (
            Decimal(0)
            if order is None
            else sum(
                (f.quantity for f in repo.list_fills(broker_order_id=order.broker_order_id)),
                Decimal(0),
            )
        )
        owned -= filled
        if exit_intent.intent_id != intent.intent_id and exit_intent.state in {
            IntentState.SUBMITTING,
            IntentState.SUBMISSION_UNKNOWN,
            IntentState.ORDER_CREATED,
            IntentState.RISK_APPROVED,
        }:
            owned -= exit_intent.quantity - filled
    return ("CORE_EXIT_OWNED_QUANTITY_UNAVAILABLE",) if intent.quantity > owned else ()


def _discovery_elapsed(decision: Decision, now: datetime) -> bool:
    """Only elapsed retained authority is permanent; future/invalid evidence is not reclassified."""
    from quantbot.paper_core.discovery import require_fresh_discovery

    discovery = decision.selection.cohort.discovery if decision.selection else None
    if discovery is None:
        return False
    stamps = [discovery.recorded_at, discovery.catalog_at]
    stamps.extend(
        at for sample in discovery.samples for at in (sample.source_at, sample.received_at)
    )
    try:
        if any(at > now for at in stamps):
            return False
        require_fresh_discovery(discovery, now)
    except ValueError as error:
        return str(error) == "DISCOVERY_RECEIPT_STALE"
    except TypeError:
        return False
    return False


def _retirement_elapsed(decision: Decision, plan: ProtectionPlan, now: datetime) -> bool:
    return bool(_retirement_reasons(decision, plan, now))


def _retirement_reasons(decision: Decision, plan: ProtectionPlan, now: datetime) -> tuple[str, ...]:
    reasons = []
    if now >= decision.valid_until:
        reasons.append("CORE_ENTRY_DECISION_EXPIRED")
    if now >= plan.deadline:
        reasons.append("CORE_PROTECTION_DEADLINE_DUE")
    if now >= decision.session_close - timedelta(seconds=plan.exit_buffer_seconds):
        reasons.append("CORE_SESSION_EXIT_BUFFER_DUE")
    if _discovery_elapsed(decision, now):
        reasons.append("DISCOVERY_AUTHORITY_EXPIRED")
    return tuple(reasons)


def _latched_refusal(repo: StorageRepository, intent_id: str) -> bool:
    return any(
        e.event_type == REFUSAL
        and e.detail.get("definitively_unsent") is True
        and e.detail.get("state_at_refusal") == IntentState.ORDER_CREATED.value
        and e.detail.get("guard_contract") == "core-final-send-v1"
        for e in repo.list_order_events(intent_id=intent_id)
    )


def retire_stale_entries(
    store: CoreStore, *, clock: Callable[[], datetime], intent_id: str | None = None
) -> tuple[str, ...]:
    """A durable pre-send state proves no POST was possible; absence/404 does not."""
    retired = []
    links = table("core_intent_links")
    intents = table("order_intents")
    with store.database.transaction() as session:
        if intent_id is not None:
            # A negative screen grants no send authority. Healthy acquisition need
            # not compete for the writer; possible retirement is revalidated below.
            repo = StorageRepository(session)
            intent = repo.get_order_intent(intent_id)
            if intent is None or intent.state not in {
                IntentState.RISK_APPROVED,
                IntentState.ORDER_CREATED,
            }:
                return ()
            if repo.get_broker_order_by_client_order_id(intent.client_order_id) is not None:
                return ()
            link = (
                session.execute(
                    select(links).where(links.c.intent_id == intent_id, links.c.role == "ENTRY")
                )
                .mappings()
                .one_or_none()
            )
            if link is None:
                return ()
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
            if not _latched_refusal(repo, intent_id) and not _retirement_elapsed(
                decision, plan, clock()
            ):
                return ()
        candidates = (
            session.execute(
                select(links.c.intent_id)
                .join(intents, links.c.intent_id == intents.c.intent_id)
                .where(
                    links.c.role == "ENTRY",
                    *([links.c.intent_id == intent_id] if intent_id is not None else []),
                    intents.c.state.in_(
                        (IntentState.RISK_APPROVED.value, IntentState.ORDER_CREATED.value)
                    ),
                )
            )
            .scalars()
            .all()
        )
    # Each refusal is an independent lifecycle transition. Release the writer
    # between candidates so an expired batch cannot block protection or quotes.
    for identity in candidates:
        with store.database.transaction() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            repo = StorageRepository(session)
            link = (
                session.execute(
                    select(links).where(links.c.intent_id == identity, links.c.role == "ENTRY")
                )
                .mappings()
                .one_or_none()
            )
            if link is None:
                continue
            intent = repo.get_order_intent(link["intent_id"])
            if intent is None or intent.state not in {
                IntentState.RISK_APPROVED,
                IntentState.ORDER_CREATED,
            }:
                continue
            if repo.get_broker_order_by_client_order_id(intent.client_order_id) is not None:
                continue
            if store.config.evidence_lane == "BROKER_PAPER":
                from quantbot.paper_core.authority import runtime_objections

                # Cleanup may run while entries are fenced, but never as a stale
                # executable or an uncommissioned writer of successor state.
                if runtime_objections(store, session, entry=False):
                    continue
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
            handoff = EntryIntent.model_validate_json(link["payload"])
            if (
                decision.id != link["decision_id"]
                or plan.id != link["protection_id"]
                or plan.decision_id != decision.id
                or decision.config_id != store.config_id
                or decision.observation_id != link["observation_id"]
                or plan.owner != store.config.owner
                or plan.generation != store.config.generation
                or handoff.decision_id != decision.id
                or handoff.protection_id != plan.id
                or handoff.intent.model_copy(update={"state": intent.state}) != intent
                or intent.strategy_id != store.config.owner
                or intent.symbol != decision.symbol
                or decision.action != "BUY"
            ):
                continue
            now = clock()
            latched = _latched_refusal(repo, intent.intent_id)
            if not latched and any(
                e.event_type == REFUSAL for e in repo.list_order_events(intent_id=intent.intent_id)
            ):
                # A malformed/nondefinitive prior marker is not authority to
                # manufacture a definitive replacement or rewrite its history.
                continue
            reasons = _retirement_reasons(decision, plan, now)
            if not latched and (
                not reasons or decision.recorded_at_utc > now or plan.recorded_at_utc > now
            ):
                continue
            if intent.state is IntentState.RISK_APPROVED:
                intent = repo.transition_order_intent(
                    intent.intent_id,
                    IntentState.ORDER_CREATED,
                    expected_current=IntentState.RISK_APPROVED,
                )
            if not latched:
                record_pre_send_refusal(
                    session,
                    intent,
                    OrderPurpose.ENTRY,
                    reasons,
                    clock=clock,
                    evidence_ids=(decision.id, plan.id),
                    source="core-stale-entry-retirement-v1",
                    evaluation_scope="PERMANENT_EXPIRY_ONLY",
                )
            repo.transition_order_intent(
                intent.intent_id, IntentState.ERROR, expected_current=IntentState.ORDER_CREATED
            )
            retired.append(intent.intent_id)
    return tuple(retired)
