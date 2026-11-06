"""One causal COLLECT decision and durable entry/protection claim, before coordinator POST."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text

from quantbot.brokers.base import BrokerProtocolError, BrokerRequestError, BrokerTransportError
from quantbot.domain import OrderIntent, OrderSide, OrderType, TimeInForce
from quantbot.execution.coordinator import OrderSubmissionBlocked
from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
from quantbot.paper_core.asset_capability import capability_for, observation_capability_objections
from quantbot.paper_core.contracts import (
    CoreConfig,
    Decision,
    EntryIntent,
    ModelArtifact,
    Observation,
    ObservationCohort,
    ProtectionPlan,
    ReconciliationProof,
    TimeProjection,
)
from quantbot.paper_core.entry_policy import (
    EntryPolicy,
    decision_entry_authority,
    eligibility_objections,
    entry_policy_in_session,
    entry_quantity,
    symbols,
)
from quantbot.paper_core.features import observation_objections
from quantbot.paper_core.funds import entry_inventory_objections, structural_funds_allowed
from quantbot.paper_core.health import entry_health_objections, report
from quantbot.paper_core.runtime import coordinator, require_runtime_broker
from quantbot.paper_core.send_guard import definitively_unsent
from quantbot.paper_core.simulation import SyntheticBroker
from quantbot.paper_core.store import CoreStore, latest_certification, put, table
from quantbot.paper_core.time_evidence import (
    TimeEvidence,
    acquire_time_evidence,
    entry_time_objections,
    project_time_evidence,
)
from quantbot.research.manifest import canonical_result_json, content_id
from quantbot.risk import DrawdownState, OrderPurpose, PortfolioVetoDecision
from quantbot.storage import StateConflictError, StorageRepository


def record_observation(store: CoreStore, observation: Observation) -> str:
    return store.record("core_observations", observation)


def _emit_inventory_diagnostic(payload: str, *, flush: bool = True) -> None:
    try:
        print(payload, flush=flush)
    except (OSError, ValueError):
        pass  # Output availability cannot grant or refuse acquisition authority.


def evaluate_batch(
    store: CoreStore,
    cohort: ObservationCohort,
    model: ModelArtifact,
    proof: ReconciliationProof,
    *,
    now: datetime,
) -> tuple[Decision, ...]:
    from quantbot.paper_core.batch import evaluate_batch as evaluate

    return evaluate(store, cohort, model, proof, now=now)


def claim_batch(
    store: CoreStore, decisions: tuple[Decision, ...], *, now: datetime
) -> tuple[EntryIntent, ...]:
    from quantbot.paper_core.batch import claim_batch as claim

    return claim(store, decisions, now=now)


def evaluate_entry(
    config: CoreConfig,
    observation: Observation,
    model: ModelArtifact,
    inventory: ReconciliationProof,
    *,
    time_evidence: TimeEvidence | None = None,
    time_projection: TimeProjection | None = None,
    activation: object | None = None,
    _cohort_member: bool = False,
    entry_policy: EntryPolicy | None = None,
    available_notional: Decimal | None = None,
) -> Decision:
    evaluated_at = (
        time_projection.wall_at
        if time_projection is not None
        else time_evidence.wall_anchor
        if time_evidence is not None
        else observation.recorded_at_utc
    )
    time_reasons: tuple[str, ...] = ()
    quote_interval = None
    if config.clock_profile == "PAPER_TIME_V2":
        time_reasons = (
            ("TIME_EVIDENCE_MISSING",)
            if time_evidence is None
            else entry_time_objections(
                observation, time_evidence, config, projection=time_projection
            )
        )
        if not time_reasons and time_evidence is not None:
            quote_interval = (
                (time_projection.provider_earliest, time_projection.provider_latest)
                if time_projection
                else (
                    evaluated_at + timedelta(seconds=time_evidence.offset_lower),
                    evaluated_at + timedelta(seconds=time_evidence.offset_upper),
                )
            )
    objections = list(
        observation_objections(
            observation,
            dependency_freshness=config.clock_profile == "PAPER_TIME_V2",
            evaluation_at=evaluated_at,
            provider_interval=quote_interval,
        )
    )
    objections.extend(observation_capability_objections(config, observation))
    capability = capability_for(config, observation.symbol)
    if capability is not None and model.mode != "COLLECT":
        objections.append("OFFLINE_CAPABILITY_COLLECT_ONLY")
    objections.extend(time_reasons)
    crypto = observation.crypto_session is not None
    if crypto and (
        entry_policy is None
        or entry_policy.crypto is None
        or observation.symbol not in entry_policy.crypto.symbols
    ):
        objections.append("CRYPTO_POLICY_NOT_COMMISSIONED")
    hybrid = config.selection_policy == "SIGNAL_OR_EXPLORE_V1"
    if hybrid and not _cohort_member:
        objections.append("SELECTION_COHORT_REQUIRED")
    if (
        hybrid
        and not crypto
        and time_evidence is not None
        and time_evidence.samples
        and (observation.session_open != time_evidence.samples[-1].provider.session_open)
    ):
        objections.append("TIME_CALENDAR_MISMATCH")
    if (
        not (
            entry_policy.permits(observation.symbol)
            if hybrid and entry_policy
            else observation.symbol
            in (
                symbols(config, entry_policy)
                if hybrid or config.asset_capabilities
                else (config.symbol,)
            )
        )
        or model.feature_version != observation.feature_version
        and not (
            (observation.feature_context == "OPENING_QUOTE_GAP_V1" or crypto)
            and model.mode == "COLLECT"
        )
    ):
        objections.append("CONTRACT_MISMATCH")
    from quantbot.paper_core.activation import Activation

    selection = activation if isinstance(activation, Activation) else None
    if hybrid and selection is None:
        objections.append("CORE_MODEL_UNCOMMISSIONED")
    if crypto and model.mode != "COLLECT":
        objections.append("CRYPTO_MODEL_PROCEDURE_NOT_VALIDATED")
    if crypto:
        # A declared experimental momentum screen, not a prediction of profit.
        # Price movement smaller than the modeled fees is not a reason to collect
        # another small-target scalp. Actual fees remain independently unknown.
        values = dict(observation.features)
        cost_bps = (
            Decimal(2 * entry_policy.crypto.estimated_taker_fee_bps + 10)
            if entry_policy is not None and entry_policy.crypto is not None
            else Decimal(60)
        )
        if (
            values.get("return_5m", 0) <= 0
            or values.get("return_15m", 0) * 10000 <= float(cost_bps)
            or values.get("relative_strength_15m", 0) < 0
        ):
            objections.append("CRYPTO_COST_AWARE_SIGNAL_NOT_QUALIFIED")
    opening = observation.feature_context in {"OPENING_CONTIGUOUS_V1", "OPENING_QUOTE_GAP_V1"}
    if opening:
        if model.mode != "COLLECT":
            objections.append("OPENING_CONTEXT_MODEL_UNSUPPORTED")
        values = dict(observation.features)
        # Explicit experimental opening screen, not a learned profit forecast.
        # Do not buy merely because an opening observation exists.
        qualified = (
            values.get("return_5m", 0) > 0
            and values.get("return_15m", 0) > 0.001
            and values.get("relative_strength_15m", 0) > 0
        )
        if observation.feature_context == "OPENING_QUOTE_GAP_V1":
            qualified = (
                values.get("gap_return", 0) > 0.001
                and values.get("relative_gap", 0) > 0
                and 0 <= values.get("spread_bps", float("inf")) <= 10
            )
        if not qualified:
            objections.append("OPENING_SIGNAL_NOT_QUALIFIED")
    prediction = None
    if model.mode != "COLLECT" and not opening:
        if (
            selection is None
            or selection.model_id != model.id
            or selection.model_digest != model.id
        ):
            objections.append("SHADOW_MODEL_NOT_ACTIVATED")
        else:
            from quantbot.paper_core.learning import predict_bps

            try:
                prediction = predict_bps(model, (dict(observation.features),))[0]
            except (KeyError, ValueError, OverflowError):
                objections.append("PREDICT_FEATURES_INVALID")
            else:
                if prediction <= 10 and not hybrid:
                    objections.append("PREDICT_NET_RESERVE")
    if (
        inventory.account.account_id != config.account_id
        or not inventory.quantity_scope_shape_valid
        or not structural_funds_allowed(config, inventory)
    ):
        objections.append("INVENTORY_UNCERTIFIED")
    if observation.symbol in inventory.quarantined_symbols:
        objections.append("SYMBOL_CUSTODY_QUARANTINED")

    if not 0 <= (evaluated_at - inventory.recorded_at_utc).total_seconds() <= 120:
        objections.append("INVENTORY_STALE")
    if dict(inventory.actual_quantities).get(observation.symbol, Decimal(0)) != 0:
        objections.append("SYMBOL_ALREADY_HELD")
    eligibility = observation.asset_eligibility
    if entry_policy is not None:
        objections.extend(
            eligibility_objections(
                eligibility, symbol=observation.symbol, now=evaluated_at, policy_id=entry_policy.id
            )
        )
    quantity = entry_quantity(
        inventory.account,
        observation.price,
        policy=entry_policy,
        fractionable=eligibility.fractionable if eligibility else None,
        config=config,
        available_notional=available_notional,
        capability=capability,
        eligibility=eligibility,
        at=evaluated_at,
    )
    if crypto and eligibility is not None:
        objections.extend(
            eligibility_objections(
                eligibility,
                symbol=observation.symbol,
                now=evaluated_at,
                quantity=quantity,
                policy_id=entry_policy.id if entry_policy else None,
            )
        )
        if inventory.account.non_marginable_buying_power is None or (
            quantity * observation.price > inventory.account.non_marginable_buying_power
        ):
            objections.append("CRYPTO_NON_MARGINABLE_BUYING_POWER")
    if capability is not None:
        objections.extend(observation_capability_objections(config, observation, quantity=quantity))
    if quantity <= 0 or quantity * observation.price * Decimal(
        "0.01"
    ) > inventory.account.equity * Decimal("0.0025"):
        objections.append("ENTRY_RISK_LIMIT")
    if (
        quantity * observation.price > inventory.account.cash
        or quantity * observation.price > inventory.account.buying_power * Decimal("0.95")
    ):
        objections.append("BUYING_POWER_RESERVE")
    if not crypto and observation.recorded_at_utc >= observation.session_close - timedelta(
        seconds=120
    ):
        objections.append("SESSION_EXIT_BUFFER")
    timing: dict[str, Any] = {}
    if crypto:
        timing["market_calendar"] = "CRYPTO24"
        timing["modeled_round_trip_cost_bps"] = cost_bps
        timing["research_signal_contract"] = "CRYPTO_COST_SCREEN_V1"
    if entry_policy is not None:
        timing["entry_policy_id"] = entry_policy.id
    if selection is not None:
        timing.update(
            activation_id=selection.id,
            model_generation=selection.generation,
            model_digest=model.id,
            prediction_bps=prediction,
            model_mode=model.mode,
        )
        if evaluated_at < selection.at:
            objections.append("CORE_MODEL_UNAVAILABLE_AT_DECISION")
    if config.clock_profile == "PAPER_TIME_V2" and time_evidence is not None:
        timing.update(
            {
                "time_evidence_id": time_evidence.id,
                "clock_process_id": time_evidence.process_id,
                "monotonic_anchor": time_projection.monotonic_at
                if time_projection
                else time_evidence.acquired_monotonic,
                "provider_deadline": min(
                    (
                        time_projection.provider_earliest
                        if time_projection
                        else time_evidence.wall_anchor
                        + timedelta(seconds=time_evidence.offset_lower)
                    )
                    + timedelta(minutes=config.horizon_minutes),
                    evaluated_at + timedelta(minutes=config.horizon_minutes + 1)
                    if crypto
                    else observation.session_close - timedelta(seconds=120),
                ),
            }
        )
        if time_projection is not None:
            timing["time_projection"] = time_projection
    if observation.context_snapshot_id is not None:
        timing["context_snapshot_id"] = observation.context_snapshot_id
    return Decision(
        **timing,
        recorded_at_utc=evaluated_at,
        evidence_lane=config.evidence_lane,
        release_id=config.release_id,
        source_refs=(observation.id, model.id, inventory.id)
        + ((time_evidence.id,) if time_evidence else ())
        + ((entry_policy.id,) if entry_policy else ()),
        observation_id=observation.id,
        policy_id=config.policy_id,
        model_id=model.id,
        learning_receipt_id=selection.receipt_id
        if selection and selection.receipt_id
        else model.learning_receipt_id,
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
        if not store.runtime_entry_enabled:
            return None
        entry_policy = entry_policy_in_session(store, session)
        policy_reasons, minimum_share = decision_entry_authority(
            store, session, decision, now=store.clocks.sample()[0]
        )
        if policy_reasons:
            return None
        from quantbot.paper_core.activation import active_in_session, decision_model_objections

        if decision_model_objections(store, session, decision):
            return None
        from quantbot.paper_core.selection import (
            cohort_symbols,
            evaluate_cohort,
            reservation_objections,
            reservation_state,
        )

        if reservation_objections(store, session, decision):
            return None
        selection = active_in_session(store, session)
        repo = StorageRepository(session)
        if entry_health_objections(store, session, decision.symbol):
            return None
        if repo.get_kill_switch_state().engaged:
            return None
        certification = latest_certification(session)
        revoked = table("core_cash_authority_revocations")
        if (
            certification is None
            or session.execute(
                select(revoked.c.snapshot_id).where(
                    revoked.c.snapshot_id == "core-certified-" + certification[1]
                )
            ).first()
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
                    table("core_reconciliation_proofs").c.id == certification[1]
                )
            ).scalar_one()
        )
        if entry_inventory_objections(
            store,
            session,
            certified,
            now=(
                store.clocks.sample()[0]
                if store.config.evidence_lane == "BROKER_PAPER"
                else decision.recorded_at_utc
            ),
        ):
            return None
        from quantbot.paper_core.send_guard import compatible_successor, current_risk_allowed

        if not compatible_successor(
            store, session, decision, certified, None, now=store.clocks.sample()[0]
        ):
            return None
        if store.runtime_role is not None or store.config.evidence_lane == "BROKER_PAPER":
            if not current_risk_allowed(
                certified,
                quantity=decision.quantity,
                price=decision.price,
                minimum_whole_share=minimum_share,
            ):
                return None
        original = ReconciliationProof.model_validate_json(
            session.execute(
                select(table("core_reconciliation_proofs").c.payload).where(
                    table("core_reconciliation_proofs").c.id == decision.inventory_id
                )
            ).scalar_one()
        )
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

            live_projection = project_time_evidence(time_evidence, store.clocks)
            if evidence_objections(time_evidence, store.clocks) or entry_time_objections(
                observed, time_evidence, store.config, projection=live_projection
            ):
                return None
        authority: dict[str, Any] = dict(
            time_evidence=time_evidence,
            time_projection=decision.time_projection,
            activation=selection[1] if selection else None,
            entry_policy=entry_policy,
        )
        model = (
            selection[0] if selection else ModelArtifact(feature_version=observed.feature_version)
        )
        if store.config.selection_policy == "SIGNAL_OR_EXPLORE_V1":
            if decision.selection is None:
                return None
            cohort = decision.selection.cohort
            from quantbot.paper_core.selection import cohort_document

            document = cohort_document(store, cohort)
            captured = session.execute(
                select(table("core_provider_acquisitions").c.payload).where(
                    table("core_provider_acquisitions").c.id == content_id(document)
                )
            ).scalar_one_or_none()
            if captured != canonical_result_json(document):
                raise ValueError("selection does not match a captured cohort")
            for member in cohort.observations:
                payload = session.execute(
                    select(table("core_observations").c.payload).where(
                        table("core_observations").c.id == member.id
                    )
                ).scalar_one_or_none()
                if payload is None or Observation.model_validate_json(payload) != member:
                    raise ValueError("selection observation is not captured")
            daily, prior, _ = reservation_state(store, session, cohort)
            replay = evaluate_cohort(
                store.config,
                cohort,
                model,
                original,
                session_counts=daily,
                prior_counts=prior,
                health_reasons={
                    s: entry_health_objections(store, session, s)
                    for s in cohort_symbols(store.config, cohort, entry_policy)
                },
                **authority,
            )
        else:
            replay = evaluate_entry(
                store.config,
                observed,
                model,
                original,
                **authority,
            )
        if decision != replay:
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
        for link in session.execute(
            select(links).where(
                links.c.role == "ENTRY",
                links.c.lot_id.not_in(select(table("core_round_trips").c.id)),
            )
        ).mappings():
            if not definitively_unsent(repo, link["intent_id"]):
                return None
        from quantbot.paper_core.work import current_receipts

        receipts = current_receipts(session)
        from quantbot.paper_core.authority import guardian_objections, runtime_objections

        if store.config.evidence_lane == "BROKER_PAPER" and runtime_objections(
            store, session, entry=True
        ):
            return None
        # A receipt can be published after the decision projection. Relative-time
        # claims use the current execution clock, including standalone fixtures.
        readiness_at = (
            store.clocks.sample()[0]
            if time_evidence is not None
            or store.runtime_role is not None
            or store.config.evidence_lane == "BROKER_PAPER"
            else decision.recorded_at_utc
        )
        if (store.runtime_role is not None or store.config.evidence_lane == "BROKER_PAPER") and (
            not decision.recorded_at_utc <= readiness_at < decision.valid_until
            or not plan.recorded_at_utc <= readiness_at < plan.deadline
            or readiness_at >= decision.session_close - timedelta(seconds=plan.exit_buffer_seconds)
        ):
            return None
        if guardian_objections(store, session, now=readiness_at):
            return None
        if not any(
            r.owner == plan.owner
            and r.generation == plan.generation
            and r.recorded_at_utc <= readiness_at < r.valid_until
            for r in receipts
        ):
            return None
        if observed.id != decision.observation_id:
            raise ValueError("observation digest mismatch")
        digest = (
            content_id({"boundary": decision.selection.boundary_id, "decision": decision.id})
            if decision.selection is not None
            else content_id({"binding": store.config_id, "observation": decision.observation_id})
        )
        capability = capability_for(store.config, decision.symbol)
        intent = OrderIntent(
            intent_id="core-entry-" + digest,
            client_order_id="qb-" + digest[:40],
            strategy_id=store.config.owner,
            signal_date=decision.recorded_at_utc.date(),
            symbol=decision.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            time_in_force=(
                capability.time_in_force
                if capability is not None
                else TimeInForce.GTC
                if decision.market_calendar == "CRYPTO24"
                else TimeInForce.DAY
            ),
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
        from quantbot.paper_core.work import dirty

        dirty(session, (decision.id,), available_at=decision.recorded_at_utc, _initial_claim=True)
        return wrapper


async def _acquire_entry_authority(
    store: CoreStore,
    broker: SyntheticBroker | AlpacaCoreBroker,
    observation: Observation,
    cohort: ObservationCohort | None,
    inventory: ReconciliationProof,
) -> tuple[ReconciliationProof, TimeEvidence | None, TimeProjection | None, tuple[str, ...]]:
    time_evidence = None
    time_projection = None
    acquisition_reasons: tuple[str, ...] = ()
    if store.config.clock_profile == "PAPER_TIME_V2":
        time_evidence = await acquire_time_evidence(broker, store.clocks, context=store.config)
        store.record("core_time_evidence", time_evidence)
        store.runtime_time_evidence_id = time_evidence.id
        acquisition_reasons = time_evidence.objections
        if not acquisition_reasons:
            from quantbot.paper_core.guardian import synchronize

            acquisition_started = store.clocks.sample()[1]
            try:
                async with asyncio.timeout(5):
                    for attempt in range(2):
                        try:
                            await synchronize(store, broker, now=store.clocks.sample()[0])
                            break
                        except TimeoutError as error:
                            if attempt or str(error) != "RECONCILIATION_AUTHORITY_CHANGED":
                                raise
                inventory = store.latest_proof()
                _emit_inventory_diagnostic(
                    json.dumps(
                        {
                            "event": "TRADER_INVENTORY_ACQUISITION_COMPLETE",
                            "at": store.clocks.sample()[0].isoformat(),
                            "elapsed_seconds": max(
                                0, store.clocks.sample()[1] - acquisition_started
                            ),
                            "phases": [
                                {"phase": phase, "after_seconds": max(0, at - acquisition_started)}
                                for phase, at in getattr(broker, "inventory_diagnostic_trace", ())
                            ],
                        }
                    ),
                    flush=True,
                )
            except (TimeoutError, OSError, BrokerRequestError, BrokerTransportError) as error:
                acquisition_reasons = ("INVENTORY_ACQUISITION_RETRYABLE",)
                detail = str(error) if isinstance(error, TimeoutError) else ""
                _emit_inventory_diagnostic(
                    json.dumps(
                        {
                            "event": "TRADER_INVENTORY_ACQUISITION_REFUSED",
                            "at": store.clocks.sample()[0].isoformat(),
                            "exception": type(error).__name__,
                            "reason": detail
                            if detail
                            in {
                                "INVENTORY_ACQUISITION_UNSTABLE",
                                "RECONCILIATION_AUTHORITY_CHANGED",
                                "INVENTORY_CAPTURE_CROSSED_OWNED_SEND",
                            }
                            else None,
                            "elapsed_seconds": max(
                                0, store.clocks.sample()[1] - acquisition_started
                            ),
                            "phases": [
                                {"phase": phase, "after_seconds": max(0, at - acquisition_started)}
                                for phase, at in getattr(broker, "inventory_diagnostic_trace", ())
                            ],
                        }
                    ),
                    flush=True,
                )
            except (ValueError, BrokerProtocolError):
                acquisition_reasons = ("INVENTORY_PROTOCOL_INVALID",)
            except StateConflictError as error:
                acquisition_reasons = (str(error),)
        time_projection = project_time_evidence(time_evidence, store.clocks)
        acquisition_reasons = tuple(
            dict.fromkeys((*acquisition_reasons, *time_projection.objections))
        )
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
                scope="ACCOUNT" if cohort else "SYMBOL",
                scope_id=store.config.account_id if cohort else observation.symbol,
            )
        elif cohort is not None or (
            not entry_time_objections(
                observation, time_evidence, store.config, projection=time_projection
            )
            and not observation_objections(
                observation,
                dependency_freshness=True,
                evaluation_at=time_projection.wall_at,
                provider_interval=(
                    time_projection.provider_earliest,
                    time_projection.provider_latest,
                ),
            )
        ):
            from quantbot.paper_core.guardian import acknowledge_handoffs

            if (
                getattr(store, "runtime_role", None) is None
                and store.config.evidence_lane != "BROKER_PAPER"
            ):
                acknowledge_handoffs(store, now=store.clocks.sample()[0])
            from quantbot.paper_core.health import recover_health_many

            fresh_symbols = tuple(
                member.symbol
                for member in (cohort.observations if cohort else (observation,))
                if not entry_time_objections(
                    member, time_evidence, store.config, projection=time_projection
                )
                and not observation_objections(
                    member,
                    dependency_freshness=True,
                    evaluation_at=time_projection.wall_at,
                    provider_interval=(
                        time_projection.provider_earliest,
                        time_projection.provider_latest,
                    ),
                )
            )
            recover_health_many(
                store, inventory, now=store.clocks.sample()[0], symbols=fresh_symbols
            )
    return inventory, time_evidence, time_projection, acquisition_reasons


async def trader_batch_cycle(
    store: CoreStore,
    broker: SyntheticBroker | AlpacaCoreBroker,
    cohort: ObservationCohort,
    model: ModelArtifact,
    *,
    now: datetime,
) -> tuple[Decision, ...]:
    """Capture one full cohort, acquire ordinary authority and prepare original batch claims."""
    from quantbot.paper_core.activation import active_in_session
    from quantbot.paper_core.selection import record_cohort, validate_cohort

    require_runtime_broker(store, broker)
    if not store.parallel_entries:
        raise ValueError("batch requires the resource-bounded PAPER admission policy")
    validate_cohort(store.config, cohort, entry_policy=store.entry_policy)
    record_cohort(store, cohort)
    if not cohort.observations:
        return ()
    inventory, _, _, _ = await _acquire_entry_authority(
        store, broker, cohort.observations[0], cohort, store.latest_proof()
    )
    with store.database.transaction() as session:
        active = active_in_session(store, session)
    if active:
        model = active[0]
    decisions = evaluate_batch(store, cohort, model, inventory, now=store.clocks.sample()[0])
    claim_batch(store, decisions, now=store.clocks.sample()[0])
    # A separate guardian must acknowledge each durable plan; dispatch owns submission.
    return decisions


async def trader_cycle(
    store: CoreStore,
    broker: SyntheticBroker | AlpacaCoreBroker,
    observation: Observation | ObservationCohort,
    model: ModelArtifact,
    inventory: ReconciliationProof,
    *,
    now: datetime,
) -> Decision:
    require_runtime_broker(store, broker)
    cohort = observation if isinstance(observation, ObservationCohort) else None
    if cohort is not None:
        from quantbot.paper_core.selection import record_cohort, validate_cohort

        validate_cohort(store.config, cohort, entry_policy=store.entry_policy)
        record_cohort(store, cohort)
        if not cohort.observations:
            raise ValueError("NO_ELIGIBLE_COHORT_MEMBERS:" + repr(cohort.unavailable))
        observation = cohort.observations[0]
    assert isinstance(observation, Observation)
    inventory, time_evidence, time_projection, acquisition_reasons = await _acquire_entry_authority(
        store, broker, observation, cohort, inventory
    )
    if cohort is None:
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
    from quantbot.paper_core.activation import active_in_session

    model_reasons: tuple[str, ...] = ()
    selection = None
    try:
        with store.database.transaction() as session:
            selection = active_in_session(store, session)
    except (ValueError, StateConflictError):
        model_reasons = ("CORE_MODEL_BINDING_INVALID",)
    if selection is not None:
        model = selection[0]
    authority: dict[str, Any] = dict(
        time_evidence=time_evidence,
        time_projection=time_projection,
        activation=selection[1] if selection else None,
        entry_policy=store.entry_policy,
    )
    if cohort is not None:
        from quantbot.paper_core.selection import evaluate_cohort, reservation_state

        with store.database.transaction() as session:
            daily, prior, _ = reservation_state(store, session, cohort)
            from quantbot.paper_core.selection import cohort_symbols

            health = {
                s: entry_health_objections(store, session, s)
                for s in cohort_symbols(store.config, cohort, store.entry_policy)
            }
        decision = evaluate_cohort(
            store.config,
            cohort,
            model,
            inventory,
            session_counts=daily,
            prior_counts=prior,
            health_reasons=health,
            **authority,
        )
        observation = next(o for o in cohort.observations if o.id == decision.observation_id)
    else:
        decision = evaluate_entry(
            store.config,
            observation,
            model,
            inventory,
            **authority,
        )
    with store.database.transaction() as session:
        reasons = tuple(
            dict.fromkeys(
                (
                    *entry_health_objections(store, session, observation.symbol),
                    *decision.gates,
                    *acquisition_reasons,
                    *model_reasons,
                )
            )
        )
    if reasons:
        decision = decision.model_copy(update={"action": "WAIT", "gates": reasons})
        if not acquisition_reasons and reasons != ("PREDICT_NET_RESERVE",):
            report(
                store,
                role="TRADER",
                state="TEMPORARY_WAIT",
                reasons=reasons,
                now=store.clocks.sample()[0] if time_evidence else now,
                evidence_ids=(observation.id, inventory.id),
                scope_id=observation.symbol,
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
    if (
        getattr(store, "runtime_role", None) is not None
        or store.config.evidence_lane == "BROKER_PAPER"
    ):
        # The independent guardian must acknowledge this durable plan on its next poll.
        # The runtime dispatcher resumes the same intent after that receipt is available.
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
            scope_id=observation.symbol,
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
            scope_id=observation.symbol,
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
            scope_id=observation.symbol,
        )
    return decision
