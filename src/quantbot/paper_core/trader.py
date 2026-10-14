"""One causal COLLECT decision and durable entry/protection claim, before coordinator POST."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, text

from quantbot.brokers.base import BrokerProtocolError, BrokerRequestError, BrokerTransportError
from quantbot.domain import OrderIntent, OrderSide, OrderType, TimeInForce
from quantbot.execution.coordinator import OrderSubmissionBlocked
from quantbot.paper_core.contracts import (
    CoreConfig,
    Decision,
    EntryIntent,
    GuardianReceipt,
    ModelArtifact,
    Observation,
    ProtectionPlan,
    ReconciliationProof,
)
from quantbot.paper_core.features import observation_objections
from quantbot.paper_core.health import entry_health_objections, recover_health, report
from quantbot.paper_core.runtime import coordinator, require_synthetic
from quantbot.paper_core.send_guard import definitively_unsent
from quantbot.paper_core.simulation import SyntheticBroker
from quantbot.paper_core.store import CoreStore, latest_certification, put, table
from quantbot.paper_core.time_evidence import (
    TimeEvidence,
    acquire_time_evidence,
    entry_time_objections,
)
from quantbot.research.manifest import canonical_result_json, content_id
from quantbot.risk import DrawdownState, OrderPurpose, PortfolioVetoDecision
from quantbot.storage import StateConflictError, StorageRepository


def record_observation(store: CoreStore, observation: Observation) -> str:
    return store.record("core_observations", observation)


def evaluate_entry(
    config: CoreConfig,
    observation: Observation,
    model: ModelArtifact,
    inventory: ReconciliationProof,
    *,
    time_evidence: TimeEvidence | None = None,
) -> Decision:
    objections = list(
        observation_objections(
            observation, dependency_freshness=config.clock_profile == "PAPER_TIME_V2"
        )
    )
    if config.clock_profile == "PAPER_TIME_V2":
        objections.extend(
            ("TIME_EVIDENCE_MISSING",)
            if time_evidence is None
            else entry_time_objections(observation, time_evidence, config)
        )
    if observation.symbol != config.symbol or model.feature_version != observation.feature_version:
        objections.append("CONTRACT_MISMATCH")
    if (
        inventory.account.account_id != config.account_id
        or inventory.expected_quantities != inventory.actual_quantities
        or not inventory.complete
        or not inventory.cash_complete
        or inventory.expected_cash is None
        or inventory.account.cash != inventory.expected_cash
    ):
        objections.append("INVENTORY_UNCERTIFIED")
    evaluated_at = (
        time_evidence.wall_anchor if time_evidence is not None else observation.recorded_at_utc
    )
    if not 0 <= (evaluated_at - inventory.recorded_at_utc).total_seconds() <= 120:
        objections.append("INVENTORY_STALE")
    if dict(inventory.actual_quantities).get(observation.symbol, Decimal(0)) != 0:
        objections.append("SYMBOL_ALREADY_HELD")
    notional = inventory.account.equity * Decimal("0.005")
    quantity = (notional / observation.price).quantize(Decimal("0.000001"), rounding="ROUND_DOWN")
    if quantity <= 0 or quantity * observation.price * Decimal(
        "0.01"
    ) > inventory.account.equity * Decimal("0.0025"):
        objections.append("ENTRY_RISK_LIMIT")
    if (
        quantity * observation.price > inventory.account.cash
        or quantity * observation.price > inventory.account.buying_power * Decimal("0.95")
    ):
        objections.append("BUYING_POWER_RESERVE")
    if observation.recorded_at_utc >= observation.session_close - timedelta(seconds=120):
        objections.append("SESSION_EXIT_BUFFER")
    timing = {}
    if config.clock_profile == "PAPER_TIME_V2" and time_evidence is not None:
        timing = {
            "time_evidence_id": time_evidence.id,
            "clock_process_id": time_evidence.process_id,
            "monotonic_anchor": time_evidence.acquired_monotonic,
            "provider_deadline": min(
                time_evidence.wall_anchor + timedelta(seconds=time_evidence.offset_lower + 1800),
                observation.session_close - timedelta(seconds=120),
            ),
        }
    return Decision(
        **timing,
        recorded_at_utc=evaluated_at,
        release_id=config.release_id,
        source_refs=(observation.id, model.id, inventory.id)
        + ((time_evidence.id,) if time_evidence else ()),
        observation_id=observation.id,
        policy_id=config.policy_id,
        model_id=model.id,
        learning_receipt_id=model.learning_receipt_id,
        config_id=content_id(config),
        inventory_id=inventory.id,
        inventory_generation=inventory.generation,
        symbol=observation.symbol,
        action="WAIT" if objections else "BUY",
        gates=tuple(objections),
        quantity=quantity,
        price=observation.price,
        valid_until=observation.source_end + timedelta(seconds=120),
        session_close=observation.session_close,
        source_start=observation.source_start,
    )


def claim_entry(store: CoreStore, decision: Decision, plan: ProtectionPlan) -> EntryIntent | None:
    links = table("core_intent_links")
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        put(session, "core_decisions", decision.id, decision)
        if decision.action != "BUY":
            return None
        repo = StorageRepository(session)
        if entry_health_objections(store, session, decision.symbol):
            return None
        if repo.get_kill_switch_state().engaged:
            return None
        certs = table("core_inventory_certifications")
        latest = session.execute(
            select(certs.c.proof_id).order_by(certs.c.generation.desc()).limit(1)
        ).scalar_one()
        certification = latest_certification(session)
        revoked = table("core_cash_authority_revocations")
        if (
            certification is None
            or certification[1] != latest
            or session.execute(
                select(revoked.c.snapshot_id).where(
                    revoked.c.snapshot_id == "core-certified-" + latest
                )
            ).first()
            or decision.inventory_id != latest
            or decision.config_id != store.config_id
        ):
            return None
        observed = Observation.model_validate_json(
            session.execute(
                select(table("core_observations").c.payload).where(
                    table("core_observations").c.id == decision.observation_id
                )
            ).scalar_one()
        )
        certified = ReconciliationProof.model_validate_json(
            session.execute(
                select(table("core_reconciliation_proofs").c.payload).where(
                    table("core_reconciliation_proofs").c.id == latest
                )
            ).scalar_one()
        )
        if not store.cash_authority_current(session, certified):
            return None
        time_evidence = None
        if decision.time_evidence_id:
            time_evidence = TimeEvidence.model_validate_json(
                session.execute(
                    select(table("core_time_evidence").c.payload).where(
                        table("core_time_evidence").c.id == decision.time_evidence_id
                    )
                ).scalar_one()
            )
            from quantbot.paper_core.time_evidence import evidence_objections

            if evidence_objections(time_evidence, store.clocks):
                return None
        if decision != evaluate_entry(
            store.config, observed, ModelArtifact(), certified, time_evidence=time_evidence
        ):
            raise ValueError("decision does not match certified inputs and fixed risk envelope")
        if plan != ProtectionPlan.for_decision(decision, store.config):
            raise ValueError("protection plan does not match fixed authority")
        if session.execute(
            select(links.c.intent_id).where(
                links.c.observation_id == decision.observation_id, links.c.role == "ENTRY"
            )
        ).first():
            return None
        # Only a proven pre-SUBMITTING refusal releases the slot; ERROR alone is unsafe.
        for link in session.execute(select(links).where(links.c.role == "ENTRY")).mappings():
            if (
                session.execute(
                    select(table("core_round_trips").c.id).where(
                        table("core_round_trips").c.id == link["lot_id"]
                    )
                ).first()
                is not None
            ):
                continue
            if not definitively_unsent(repo, link["intent_id"]):
                return None
        receipts = [
            GuardianReceipt.model_validate_json(p)
            for p in session.execute(select(table("core_guardian_receipts").c.payload)).scalars()
        ]
        if not any(
            r.owner == plan.owner
            and r.generation == plan.generation
            and r.recorded_at_utc <= decision.recorded_at_utc <= r.valid_until
            for r in receipts
        ):
            return None
        if observed.id != decision.observation_id:
            raise ValueError("observation digest mismatch")
        digest = content_id({"binding": store.config_id, "observation": decision.observation_id})
        intent = OrderIntent(
            intent_id="core-entry-" + digest,
            client_order_id="qb-" + digest[:40],
            strategy_id=store.config.owner,
            signal_date=decision.recorded_at_utc.date(),
            symbol=decision.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            quantity=decision.quantity,
            created_at=decision.recorded_at_utc,
        )
        put(session, "core_protection_plans", plan.id, plan)
        repo.create_order_intent(intent)
        wrapper = EntryIntent(intent=intent, decision_id=decision.id, protection_id=plan.id)
        session.execute(
            links.insert().values(
                intent_id=intent.intent_id,
                decision_id=decision.id,
                protection_id=plan.id,
                observation_id=decision.observation_id,
                role="ENTRY",
                lot_id=decision.id,
                payload=canonical_result_json(wrapper),
            )
        )
        return wrapper


async def trader_cycle(
    store: CoreStore,
    broker: SyntheticBroker,
    observation: Observation,
    model: ModelArtifact,
    inventory: ReconciliationProof,
    *,
    now: datetime,
) -> Decision:
    require_synthetic(store, broker)
    time_evidence = None
    acquisition_reasons: tuple[str, ...] = ()
    if store.config.clock_profile == "PAPER_TIME_V2":
        time_evidence = await acquire_time_evidence(broker, store.clocks)
        store.record("core_time_evidence", time_evidence)
        acquisition_reasons = time_evidence.objections
        if not acquisition_reasons:
            from quantbot.paper_core.guardian import synchronize

            try:
                await synchronize(store, broker, now=store.clocks.sample()[0])
                inventory = store.latest_proof()
            except (TimeoutError, OSError, BrokerRequestError, BrokerTransportError):
                acquisition_reasons = ("INVENTORY_ACQUISITION_RETRYABLE",)
            except (ValueError, BrokerProtocolError):
                acquisition_reasons = ("INVENTORY_PROTOCOL_INVALID",)
            except StateConflictError as error:
                acquisition_reasons = (str(error),)
        if acquisition_reasons:
            report(
                store,
                role="TRADER",
                state="PROTOCOL_FAILURE"
                if any("PROTOCOL_INVALID" in r for r in acquisition_reasons)
                else "RETRYABLE_FAILURE",
                reasons=acquisition_reasons,
                now=store.clocks.sample()[0],
                evidence_ids=(time_evidence.id, inventory.id),
            )
        elif not entry_time_objections(
            observation, time_evidence, store.config
        ) and not observation_objections(observation, dependency_freshness=True):
            from quantbot.paper_core.guardian import acknowledge_handoffs

            acknowledge_handoffs(store, now=store.clocks.sample()[0])
            recover_health(store, inventory, now=store.clocks.sample()[0])
    record_observation(store, observation)
    previous = next(
        (
            link
            for link in store.links()
            if link["role"] == "ENTRY" and link["observation_id"] == observation.id
        ),
        None,
    )
    if previous is not None:
        return store.read("core_decisions", previous["decision_id"], Decision)
    decision = evaluate_entry(
        store.config, observation, model, inventory, time_evidence=time_evidence
    )
    with store.database.transaction() as session:
        reasons = tuple(
            dict.fromkeys(
                (
                    *entry_health_objections(store, session, observation.symbol),
                    *decision.gates,
                    *acquisition_reasons,
                )
            )
        )
    if reasons:
        decision = decision.model_copy(update={"action": "WAIT", "gates": reasons})
        if not acquisition_reasons:
            report(
                store,
                role="TRADER",
                state="TEMPORARY_WAIT",
                reasons=reasons,
                now=store.clocks.sample()[0] if time_evidence else now,
                evidence_ids=(observation.id, inventory.id),
            )
    plan = ProtectionPlan.for_decision(decision, store.config)
    if time_evidence is not None:
        now = store.clocks.sample()[0]
    if now < decision.recorded_at_utc or now >= decision.valid_until:
        store.record("core_decisions", decision)
        return decision
    entry = claim_entry(store, decision, plan)
    if entry is None:
        return decision
    from quantbot.paper_core.guardian import acknowledge_handoffs

    acknowledge_handoffs(store, now=now)
    # Synthetic fixture has explicit fixed drawdown and veto assumptions. These do not attest
    # production market/concentration readiness and cannot flow to a real broker.
    drawdown = DrawdownState(
        current_equity=inventory.account.equity,
        high_water_equity=inventory.account.equity,
        drawdown_fraction=Decimal(0),
        new_risk_multiplier=Decimal(1),
        entry_halted=False,
        liquidation_required=False,
    )
    vetoes = PortfolioVetoDecision(
        symbol=decision.symbol,
        policy_version=1,
        rule_parameters_hash="EXPLICIT_SYNTHETIC_ASSUMPTIONS",
        evaluated=(),
        rejections=(),
    )
    try:
        await coordinator(store, broker).submit_durable(
            entry.intent,
            purpose=OrderPurpose.ENTRY,
            market_eligible=True,
            drawdown=drawdown,
            risk_approved=True,
            vetoes=vetoes,
        )
    except OrderSubmissionBlocked:
        if store.config.clock_profile != "PAPER_TIME_V2":
            raise
        report(
            store,
            role="TRADER",
            state="TEMPORARY_WAIT",
            reasons=("FINAL_SEND_REFUSED",),
            now=store.clocks.sample()[0],
            evidence_ids=(decision.id, inventory.id),
        )
        return decision
    except (
        TimeoutError,
        OSError,
        BrokerRequestError,
        BrokerTransportError,
        BrokerProtocolError,
    ) as error:
        if store.config.clock_profile != "PAPER_TIME_V2":
            raise
        report(
            store,
            role="TRADER",
            state="PROTOCOL_FAILURE"
            if isinstance(error, BrokerProtocolError)
            else "RETRYABLE_FAILURE",
            reasons=("ENTRY_PROVIDER_RETRY_PENDING",),
            now=store.clocks.sample()[0],
            evidence_ids=(decision.id, inventory.id),
        )
        return decision
    from quantbot.paper_core.guardian import synchronize

    try:
        await synchronize(store, broker, now=broker.now)
    except (TimeoutError, OSError, BrokerRequestError, BrokerTransportError, StateConflictError):
        if store.config.clock_profile != "PAPER_TIME_V2":
            raise
        report(
            store,
            role="TRADER",
            state="RETRYABLE_FAILURE",
            reasons=("POST_ENTRY_RECONCILIATION_WAIT",),
            now=store.clocks.sample()[0],
            evidence_ids=(decision.id,),
        )
    return decision
