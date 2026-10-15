"""Three independent bounded roles. No role startup grants broker/model authority."""

from __future__ import annotations

import asyncio
import os
import random
from datetime import date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select

from quantbot.domain import IntentState
from quantbot.paper_core import activation
from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
from quantbot.paper_core.authority import (
    RoleHeartbeat,
    guardian_objections,
    publish_heartbeat,
    runtime_objections,
)
from quantbot.paper_core.contracts import (
    Decision,
    LearningPolicy,
    LearningReceipt,
    ProtectionPlan,
)
from quantbot.paper_core.guardian import acknowledge_handoffs, guardian_cycle
from quantbot.paper_core.outcomes import process_outcomes, recover_intents
from quantbot.paper_core.release import RuntimeConfig
from quantbot.paper_core.runtime import coordinator, require_runtime_broker
from quantbot.paper_core.send_guard import definitively_unsent
from quantbot.paper_core.store import CoreStore, table
from quantbot.paper_core.trader import claim_entry, trader_cycle
from quantbot.risk import OrderPurpose
from quantbot.storage import StorageRepository

Role = Literal["trader", "guardian", "outcomes"]


def claim_objections(store: CoreStore, decision: Decision | None = None) -> tuple[str, ...]:
    reasons: list[str] = []
    with store.database.transaction() as session:
        repo = StorageRepository(session)
        if store.config.evidence_lane == "BROKER_PAPER":
            reasons.extend(runtime_objections(store, session, entry=True))
        if repo.get_kill_switch_state().engaged:
            reasons.append("CORE_MANUAL_ENTRY_PAUSE:" + repo.get_kill_switch_state().reason)
        from quantbot.paper_core.health import entry_health_objections

        if decision is not None:
            reasons.extend(entry_health_objections(store, session, decision.symbol))
        else:
            from quantbot.paper_core.health import universe_health_objections

            reasons.extend(universe_health_objections(store, session))
        for link in store.links():
            if link["role"] != "ENTRY":
                continue
            intent = repo.get_order_intent(link["intent_id"])
            if intent is None or intent.state in {
                IntentState.SUBMITTING,
                IntentState.SUBMISSION_UNKNOWN,
            }:
                reasons.append("CORE_UNRESOLVED_SUBMISSION:" + link["intent_id"])
            elif (
                not definitively_unsent(repo, link["intent_id"])
                and not session.execute(
                    select(table("core_round_trips").c.id).where(
                        table("core_round_trips").c.id == link["lot_id"]
                    )
                ).first()
            ):
                reasons.append("CORE_ENTRY_SLOT_RESERVED:" + link["intent_id"])
        reasons.extend(guardian_objections(store, session, now=store.clocks.sample()[0]))
        try:
            selection = activation.active_in_session(store, session)
            if selection is None:
                reasons.append("ACTIVE_MODEL_UNCOMMISSIONED")
        except ValueError as error:
            reasons.append(str(error))
    if decision is not None:
        reasons.extend(decision.gates)
    return tuple(dict.fromkeys(reasons))


def owned_open_symbols(store: CoreStore) -> tuple[str, ...]:
    """Refresh marks for filled owned lots, including symbols outside today's universe."""
    with store.database.transaction() as session:
        repo = StorageRepository(session)
        links = table("core_intent_links")
        symbols = set()
        for link in session.execute(
            select(links).where(
                links.c.role == "ENTRY",
                links.c.lot_id.not_in(select(table("core_round_trips").c.id)),
            )
        ).mappings():
            intent = repo.get_order_intent(link["intent_id"])
            order = (
                repo.get_broker_order_by_client_order_id(intent.client_order_id) if intent else None
            )
            if intent and order and repo.list_fills(broker_order_id=order.broker_order_id):
                symbols.add(intent.symbol)
    return tuple(sorted(symbols))


async def dispatch_entries(store: CoreStore, broker: Any) -> tuple[str, ...]:
    reasons = []
    with store.database.transaction() as session:
        links, intents = table("core_intent_links"), table("order_intents")
        prepared = list(
            session.execute(
                select(links)
                .join(intents, links.c.intent_id == intents.c.intent_id)
                .where(
                    links.c.role == "ENTRY",
                    intents.c.state.in_(
                        (IntentState.RISK_APPROVED.value, IntentState.ORDER_CREATED.value)
                    ),
                )
            ).mappings()
        )
    for link in prepared:
        with store.database.transaction() as session:
            intent = StorageRepository(session).get_order_intent(link["intent_id"])
        if intent is None or intent.state not in {
            IntentState.RISK_APPROVED,
            IntentState.ORDER_CREATED,
        }:
            continue
        now = store.clocks.sample()[0]
        plan = store.read("core_protection_plans", link["protection_id"], ProtectionPlan)
        # Give guardian its bounded next poll to acknowledge the original durable plan.
        with store.database.transaction() as session:
            from quantbot.paper_core.work import current_receipts

            receipts = current_receipts(session, plan_id=plan.id)
            matching = [
                r
                for r in receipts
                if plan.id in r.protection_ids and r.recorded_at_utc <= now < r.valid_until
            ]
            if not matching or guardian_objections(
                store,
                session,
                now=now,
                receipt_id=max(matching, key=lambda r: r.recorded_at_utc).id,
            ):
                reasons.append("CORE_INDEPENDENT_GUARDIAN_NOT_READY")
                continue
        try:
            await coordinator(store, broker).submit_durable(
                intent,
                purpose=OrderPurpose.ENTRY,
                market_eligible=True,
                drawdown=None,
                risk_approved=None,
                vetoes=None,
            )
        except Exception as error:
            gate = getattr(error, "decision", None)
            reasons.extend(getattr(gate, "reasons", (type(error).__name__,)))
    return tuple(dict.fromkeys(reasons))


def retry_entry_candidate(store: CoreStore, candidate: Decision | None) -> Decision | None:
    """Retry one original candidate while its original authority remains live."""
    if candidate is None:
        return None
    now = store.clocks.sample()[0]
    plan = ProtectionPlan.for_decision(candidate, store.config)
    if (
        candidate.action != "BUY"
        or not candidate.recorded_at_utc <= now < candidate.valid_until
        or now >= plan.deadline
        or now >= candidate.session_close - timedelta(seconds=plan.exit_buffer_seconds)
    ):
        return None
    links = table("core_intent_links")
    with store.database.transaction() as session:
        if session.execute(
            select(links.c.intent_id).where(links.c.decision_id == candidate.id).limit(1)
        ).first():
            return None
    return None if claim_entry(store, candidate, plan) is not None else candidate


async def daily_consideration(
    store: CoreStore, broker: Any, config: RuntimeConfig
) -> tuple[str, ...]:
    from quantbot.paper_core.learning import consider_learning

    calendar = await broker.learning_calendar()
    now = store.clocks.sample()[0]
    completed = [
        s
        for s in calendar.sequence.sessions
        if s.close_at <= now and s.open_at >= config.learning_origin
    ]
    if not completed:
        return ("NO_COMPLETED_EXCHANGE_SESSION",)
    with store.database.transaction() as session:
        selection = activation.active_in_session(store, session)
    if selection is None:
        return ("ACTIVE_MODEL_UNCOMMISSIONED",)
    parent, current = selection
    if current.receipt_id:
        previous = store.read("core_learning_runs", current.receipt_id, LearningReceipt)
        with store.database.transaction() as session:
            revisions = set(
                session.execute(select(table("core_outcome_index").c.outcome_id)).scalars()
            )
        known = set(previous.considered_ids) | {identity for identity, _ in previous.exclusions}
        if previous.completed_session == completed[-1].session_date and revisions <= known:
            return ("CONSIDERED_SESSION:" + completed[-1].session_date.isoformat(), previous.reason)
    policy = LearningPolicy(
        parent_model=parent,
        evidence_lane=store.config.evidence_lane,
        dataset=config.learning_dataset,
        policy_id=store.config.policy_id,
    )
    receipt = await asyncio.to_thread(
        consider_learning,
        store,
        policy,
        cutoff=completed[-1].close_at,
        execution_clock=store.clocks,
        calendar=calendar,
    )
    if store.config.evidence_lane == "BROKER_PAPER":
        if (
            receipt.status == "AWAITING_ACTIVATION"
            and config.activation_policy != "DAILY_BOUNDED_V2"
        ):
            return ("AWAITING_EXACT_CANDIDATE_APPROVAL",)
        if receipt.status in {"NO_CHANGE", "AWAITING_ACTIVATION"}:
            if current.receipt_id == receipt.id:
                return (
                    "CONSIDERED_SESSION:" + completed[-1].session_date.isoformat(),
                    receipt.reason,
                )
            try:
                authority = activation.commission_operational_candidate(
                    store, receipt, expected_activation_id=current.id, at=store.clocks.sample()[0]
                )
                activation.activate_model(
                    store,
                    expected_model_id=parent.id,
                    candidate_id=receipt.resulting_model_id,
                    authority=authority,
                    at=store.clocks.sample()[0],
                )
            except ValueError as error:
                return (str(error),)
    return ("CONSIDERED_SESSION:" + completed[-1].session_date.isoformat(), receipt.reason)


async def run_role(
    store: CoreStore,
    broker: Any,
    config: RuntimeConfig,
    role: Role,
    *,
    cycles: int | None = None,
) -> None:
    require_runtime_broker(store, broker)
    store.runtime_role = role
    period = {
        "trader": config.entry_poll_seconds,
        "guardian": config.guardian_poll_seconds,
        "outcomes": config.outcomes_poll_seconds,
    }[role]
    last_progress: datetime | None = None
    count, failures = 0, 0
    last_boundary = None
    pending_candidate: Decision | None = None
    learning_task: asyncio.Task[tuple[str, ...]] | None = None
    last_learning_session = None
    try:
        while cycles is None or count < cycles:
            cycle_started = store.clocks.sample()[1]
            count += 1
            reasons: tuple[str, ...] = ()
            evidence_ids: tuple[str, ...] = ()
            proof_age = None
            time_proof_age = None
            guardian_receipt = None
            try:
                refresh = getattr(broker, "refresh_fixture", None)
                if refresh:
                    refresh(store)
                if role == "guardian":
                    if isinstance(broker, AlpacaCoreBroker):
                        for symbol in owned_open_symbols(store):
                            try:
                                async with asyncio.timeout(5):
                                    await broker.refresh_mark(symbol)
                            except Exception:
                                pass  # Deadline/ownership recovery remains scheduled.
                    receipt = await guardian_cycle(store, broker, now=store.clocks.sample()[0])
                    guardian_receipt = acknowledge_handoffs(store, now=store.clocks.sample()[0]).id
                    reasons, evidence_ids = receipt.reasons, (receipt.id,)
                elif role == "outcomes":
                    # Durable outcomes run before provider recovery and before any fit work.
                    batch = process_outcomes(store, cutoff=store.clocks.sample()[0])
                    evidence_ids = tuple(o.id for o in batch.outcomes)
                    reasons = batch.reasons
                    try:
                        async with asyncio.timeout(5):
                            recovery = await recover_intents(store, broker)
                        reasons += recovery.reasons
                    except Exception as error:
                        reasons = ("OUTCOME_PROVIDER_RETRY:" + type(error).__name__,)
                    if learning_task is not None and learning_task.done():
                        try:
                            learning_result = learning_task.result()
                            reasons += learning_result
                            if learning_result and learning_result[0].startswith(
                                "CONSIDERED_SESSION:"
                            ):
                                last_learning_session = date.fromisoformat(
                                    learning_result[0].split(":", 1)[1]
                                )
                        except Exception as error:
                            reasons += ("LEARNING_RETRY:" + type(error).__name__,)
                        learning_task = None
                    if learning_task is None:
                        calendar = await broker.learning_calendar()
                        completed = [
                            s
                            for s in calendar.sequence.sessions
                            if s.close_at <= store.clocks.sample()[0]
                            and s.open_at >= config.learning_origin
                        ]
                        session_key = completed[-1].session_date if completed else None
                        if session_key is not None and last_learning_session != session_key:
                            learning_task = asyncio.create_task(
                                daily_consideration(store, broker, config)
                            )
                else:
                    boundary = int(store.clocks.sample()[0].timestamp() // 300)
                    decision = None
                    if boundary != last_boundary:
                        pending_candidate = None
                        async with asyncio.timeout(5):
                            observation = (
                                await broker.observation_cohort()
                                if store.config.selection_policy == "SIGNAL_OR_EXPLORE_V1"
                                else await broker.observation()
                            )
                        model = activation.read_active_model(store)
                        decision = await trader_cycle(
                            store,
                            broker,
                            observation,
                            model,
                            store.latest_proof(),
                            now=store.clocks.sample()[0],
                        )
                        last_boundary = boundary
                        evidence_ids = (decision.id,)
                        pending_candidate = decision if decision.action == "BUY" else None
                    pending_candidate = retry_entry_candidate(store, pending_candidate)
                    send_reasons = await dispatch_entries(store, broker)
                    reasons = tuple(
                        dict.fromkeys((*claim_objections(store, decision), *send_reasons))
                    )
                try:
                    proof = store.latest_proof()
                    proof_age = max(
                        0, (store.clocks.sample()[0] - proof.recorded_at_utc).total_seconds()
                    )
                except (ValueError, AssertionError):
                    reasons += ("CURRENT_PROOF_UNAVAILABLE",)
                last_progress = store.clocks.sample()[0]
                failures = 0
            except Exception as error:
                failures += 1
                reasons = tuple(
                    dict.fromkeys((*claim_objections(store), str(error) or type(error).__name__))
                )
            now = store.clocks.sample()[0]
            if store.runtime_time_evidence_id is not None:
                from quantbot.paper_core.time_evidence import TimeEvidence

                current_time = store.read(
                    "core_time_evidence", store.runtime_time_evidence_id, TimeEvidence
                )
                evidence_ids = tuple(dict.fromkeys((*evidence_ids, current_time.id)))
                time_proof_age = max(0, store.clocks.sample()[1] - current_time.acquired_monotonic)
            heartbeat = RoleHeartbeat(
                role=role,
                release_id=store.runtime_release_id,
                config_id=store.config_id,
                store_identity=store.database.store_identity,
                owner=store.config.owner,
                generation=store.config.generation,
                process_epoch=store.clocks.process_id,
                pid=os.getpid(),
                recorded_at=now,
                valid_until=now + timedelta(seconds=min(30, 2 * period)),
                last_useful_progress=last_progress,
                first_blocker=reasons[0] if reasons else None,
                cycle_seconds=max(0, store.clocks.sample()[1] - cycle_started),
                objections=tuple(dict.fromkeys(reasons)),
                evidence_ids=evidence_ids,
                proof_age_seconds=proof_age,
                time_proof_age_seconds=time_proof_age,
                guardian_receipt_id=guardian_receipt,
            )
            publish_heartbeat(store, heartbeat)
            print(heartbeat.model_dump_json(), flush=True)
            # Backoff is per role; outcome polling has priority over fitting.
            delay = period if not failures else max(period, min(15, 2 ** min(failures - 1, 4)))
            if failures:
                delay = min(15, delay + random.uniform(0, min(0.25, delay * 0.1)))
            if cycles is None or count < cycles:
                await asyncio.sleep(max(0.001, delay - (store.clocks.sample()[1] - cycle_started)))
    finally:
        if learning_task is not None:
            await learning_task
