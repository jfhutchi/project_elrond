"""Three independent bounded roles. No role startup grants broker/model authority."""

from __future__ import annotations

import asyncio
import json
import os
import random
import sqlite3
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

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
    ProtectionPlan,
    ReconciliationProof,
)
from quantbot.paper_core.cycle_diagnostics import TraderCycleDiagnostics, emit_cycle
from quantbot.paper_core.guardian import acknowledge_handoffs, guardian_cycle, owned_deadline_wait
from quantbot.paper_core.outcomes import process_outcomes, recover_intents
from quantbot.paper_core.release import RuntimeConfig
from quantbot.paper_core.runtime import coordinator, require_runtime_broker
from quantbot.paper_core.send_guard import definitively_unsent
from quantbot.paper_core.store import CoreStore, latest_certification, table
from quantbot.paper_core.trader import claim_batch, claim_entry, trader_batch_cycle, trader_cycle
from quantbot.research.manifest import content_id
from quantbot.risk import OrderPurpose
from quantbot.storage import StorageRepository

Role = Literal["trader", "guardian", "outcomes"]

CLOSED_SESSION_IDLE = "CLOSED_SESSION_IDLE"

_TRANSIENT_ENTRY_WAIT_GATES = frozenset(
    {
        "CORE_ENTRY_FENCED",
        "CORE_INDEPENDENT_GUARDIAN_NOT_READY",
        "SESSION_OPENING_UNVERIFIED",
        "SESSION_CAPITAL_UNVERIFIED",
        "PORTFOLIO_INVENTORY_STALE",
        "INVENTORY_STALE",
        "INVENTORY_UNCERTIFIED",
        "INVENTORY_ACQUISITION_RETRYABLE",
        "CORE_CASH_AUTHORITY_UNAVAILABLE",
        "CORE_INVENTORY_AUTHORITY_UNAVAILABLE",
        "CORE_PAPER_FUNDS_SOURCE_INVALID",
    }
)


def _retry_transient_wait(
    decisions: tuple[Decision, ...], *, source_end: datetime, now: datetime
) -> bool:
    """Reevaluate unclaimed waits while the original boundary is still fresh."""
    return bool(
        decisions
        and 0 <= (now - source_end).total_seconds() <= 120
        and all(
            d.action == "WAIT" and d.gates and set(d.gates) <= _TRANSIENT_ENTRY_WAIT_GATES
            for d in decisions
        )
    )


def _idle_signature(store: CoreStore, proof: ReconciliationProof | None = None) -> str | None:
    """Local change detector, never spending or protection authority."""
    from quantbot.paper_core.funds import entry_inventory_objections
    from quantbot.paper_core.health import active_health
    from quantbot.paper_core.settlement import definitively_unsent_many, settled_zero_for_config

    with store.database.transaction() as session:
        session.execute(text("BEGIN"))
        if proof is not None and entry_inventory_objections(
            store, session, proof, now=store.clocks.sample()[0]
        ):
            return None
        repo = StorageRepository(session)
        if repo.list_unresolved_order_intents():
            return None
        links = table("core_intent_links")
        candidates = list(
            session.execute(
                select(links.c.intent_id).where(
                    links.c.role == "ENTRY",
                    links.c.lot_id.not_in(select(table("core_round_trips").c.id)),
                )
            ).scalars()
        )
        unsent = definitively_unsent_many(repo, candidates)
        for identity in candidates:
            if unsent[identity]:
                continue
            intent = repo.get_order_intent(identity)
            if (
                intent is None
                or settled_zero_for_config(
                    store.config, session, intent, cutoff=store.clocks.sample()[0]
                )
                is None
            ):
                return None
        health = active_health(session)
        certification = latest_certification(session)
        if any(
            h.protection_blocked
            or h.state in {"INTEGRITY_FAILURE", "MANUAL_PAUSE"}
            or h.state == "PROTOCOL_FAILURE"
            and not (
                h.role == "GUARDIAN"
                and h.objections == ("INVENTORY_PROTOCOL_INVALID",)
                and certification is not None
                and certification[0] > h.failed_generation
            )
            for h in health
            if h.scope != "LEARNING"
        ):
            return None
        # A newer successful inventory capture permits slower *polling* after a
        # transient malformed response. Retain the health record and all entry
        # blockers; idle heartbeats grant no authority to clear or trade through it.
        stop = repo.get_kill_switch_state()
        return content_id(
            (
                certification,
                store.ledger_state_id(session),
                (stop.engaged, stop.reason, stop.updated_at),
                tuple(h.id for h in health),
                tuple(
                    session.execute(
                        select(table("core_cash_authority_revocations").c.snapshot_id).order_by(
                            table("core_cash_authority_revocations").c.snapshot_id
                        )
                    ).scalars()
                ),
            )
        )


class ClosedSessionPoll:
    """Process-local scheduling hint. Restart and all uncertainty restore full polling."""

    def __init__(self) -> None:
        self.anchor: tuple[datetime, float] | None = None
        self.until = 0.0
        self.signature: str | None = None

    def waiting(self, store: CoreStore) -> bool:
        if self.anchor is None:
            return False
        wall, mono = store.clocks.sample()
        prior_wall, prior_mono = self.anchor
        valid = (
            prior_mono <= mono < self.until
            and abs((wall - prior_wall).total_seconds() - (mono - prior_mono)) <= 0.05
            and self.signature == _idle_signature(store)
        )
        if not valid:
            self.anchor = None
        return valid

    def observe(self, store: CoreStore, broker: Any, receipt: Any) -> None:
        from quantbot.paper_core.funds import PaperFundsEvidence
        from quantbot.paper_core.time_evidence import (
            TimeEvidence,
            evidence_objections,
            provider_interval,
        )

        self.anchor = None
        if (
            type(broker) is not AlpacaCoreBroker
            or broker.closed_session_window is None
            or receipt.proof_id is None
            or set(receipt.reasons) - {"SESSION_OPENING_UNVERIFIED"}
            or store.runtime_time_evidence_id is None
        ):
            return
        timing = store.read("core_time_evidence", store.runtime_time_evidence_id, TimeEvidence)
        if evidence_objections(timing, store.clocks):
            return
        lower, upper = provider_interval(timing, store.clocks)
        closed_at, opens_at = broker.closed_session_window
        if (
            not closed_at + timedelta(minutes=10)
            <= lower
            <= upper
            < opens_at - timedelta(seconds=120)
        ):
            return
        proof = store.latest_proof()
        if (
            proof.id != receipt.proof_id
            or proof.positions
            or any(
                quantity for _, quantity in (*proof.actual_quantities, *proof.expected_quantities)
            )
        ):
            return
        funds = store.read("core_provider_acquisitions", proof.acquisition_id, PaperFundsEvidence)
        if (
            funds.first_open_orders
            or funds.second_open_orders
            or funds.first_positions
            or funds.second_positions
        ):
            return
        signature = _idle_signature(store, proof)
        if signature is not None:
            self.anchor = store.clocks.sample()
            self.until = self.anchor[1] + min(900, (opens_at - upper).total_seconds() - 120)
            self.signature = signature


def _guardian_closed_idle(store: CoreStore) -> bool:
    """Defer redundant activity reads only while the current Guardian explicitly idles."""
    with store.database.transaction() as session:
        docs, current = table("core_role_heartbeats"), table("core_role_current")
        raw = session.execute(
            select(docs.c.payload)
            .join(current, docs.c.id == current.c.heartbeat_id)
            .where(current.c.role == "guardian")
        ).scalar_one_or_none()
    heartbeat = RoleHeartbeat.model_validate_json(raw) if raw else None
    return bool(
        heartbeat
        and heartbeat.objections == (CLOSED_SESSION_IDLE,)
        and heartbeat.guardian_receipt_id is None
        and heartbeat.release_id == store.runtime_release_id
        and heartbeat.config_id == store.config_id
        and heartbeat.store_identity == store.database.store_identity
        and heartbeat.recorded_at <= store.clocks.sample()[0] < heartbeat.valid_until
        and _idle_signature(store) is not None
    )


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

        throughput = store.parallel_entries
        if throughput:
            from quantbot.paper_core.portfolio import portfolio_budget

            reasons.extend(
                portfolio_budget(
                    store, session, store.latest_proof(), now=store.clocks.sample()[0]
                ).objections
            )
        for link in () if throughput else store.links():
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
        rows = list(
            session.execute(
                select(links).where(
                    links.c.role == "ENTRY",
                    links.c.lot_id.not_in(select(table("core_round_trips").c.id)),
                )
            ).mappings()
        )
        intents = repo.get_order_intents([link["intent_id"] for link in rows])
        orders = iter(
            repo.get_broker_orders_by_client_order_ids(
                [intent.client_order_id for intent in intents if intent is not None]
            )
        )
        for intent in intents:
            order = next(orders) if intent is not None else None
            if intent and order and repo.list_fills(broker_order_id=order.broker_order_id):
                symbols.add(intent.symbol)
    return tuple(sorted(symbols))


async def dispatch_entries(
    store: CoreStore, broker: Any, *, observed: TraderCycleDiagnostics | None = None
) -> tuple[str, ...]:
    reasons: list[str] = []
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
        if store.parallel_entries:
            from quantbot.paper_core.portfolio import portfolio_budget

            with store.database.transaction() as session:
                budget = portfolio_budget(
                    store, session, store.latest_proof(), now=now, exclude_intent=intent.intent_id
                )
            if budget.objections:
                # Account-wide current authority must catch up after an earlier fill.
                # Keep the other original intents prepared; never relabel this as a quota.
                reasons.extend(budget.objections)
                break
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
            if observed is not None:
                observed.submission_requests += 1
            result = await coordinator(store, broker).submit_durable(
                intent,
                purpose=OrderPurpose.ENTRY,
                market_eligible=True,
                drawdown=None,
                risk_approved=None,
                vetoes=None,
            )
        except Exception as error:
            gate = getattr(error, "decision", None)
            if gate is None and observed is not None:
                observed.dispatch_failed = True
            fallback: tuple[str, ...] = (type(error).__name__,)
            if store.parallel_entries and str(error):
                fallback += (str(error),)
            reasons.extend(getattr(gate, "reasons", fallback))
        else:
            if observed is not None:
                with suppress(Exception):
                    observed.acknowledge(result, now=store.clocks.sample()[0])
    return tuple(dict.fromkeys(reasons))


def retry_batch_candidates(
    store: CoreStore, candidates: tuple[Decision, ...]
) -> tuple[Decision, ...]:
    """Retry the original atomic prefix, never manufacture replacement member identities."""
    now = store.clocks.sample()[0]
    if not candidates or any(
        d.action != "BUY"
        or not d.recorded_at_utc <= now < d.valid_until
        or now >= ProtectionPlan.for_decision(d, store.config).deadline
        for d in candidates
    ):
        return ()
    return () if claim_batch(store, candidates, now=now) else candidates


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
    gross = store.config.selection_policy == "SIGNAL_OR_EXPLORE_V1"
    policy = LearningPolicy(
        procedure_version=(
            "daily-paper-exploration-walk-forward-v3"
            if gross
            else "daily-all-history-walk-forward-v2"
        ),
        target_contract=(
            "owned-executed-paper-gross-return-v1" if gross else "owned-executed-net-return-v1"
        ),
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
        if receipt.status == "AWAITING_ACTIVATION" and config.activation_policy != (
            "DAILY_BOUNDED_V3" if gross else "DAILY_BOUNDED_V2"
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
    pending_batch: tuple[Decision, ...] = ()
    learning_task: asyncio.Task[tuple[str, ...]] | None = None
    discovery_task = None
    close_capture = None
    observed = TraderCycleDiagnostics()
    closed_poll = ClosedSessionPoll()
    idle_anchor: str | None = None
    idle_previous: RoleHeartbeat | None = None
    if role == "outcomes" and isinstance(broker, AlpacaCoreBroker):
        from pathlib import Path

        from quantbot.paper_core.close_evidence import OutcomesCloseCapture, configured_directory

        try:
            directory = configured_directory(
                config.environment_file_reference, Path(store.database.path)
            )
            if directory is not None:
                close_capture = OutcomesCloseCapture(store, broker, directory)
        except (OSError, ValueError):
            print(json.dumps({"event": "PAPER_CLOSE_CONFIGURATION_UNAVAILABLE"}), flush=True)
    if (
        role == "trader"
        and isinstance(broker, AlpacaCoreBroker)
        and broker.discovery_state is not None
    ):
        from quantbot.paper_core.discovery import run_discovery

        discovery_task = asyncio.create_task(run_discovery(broker, broker.discovery_state))
    try:
        while cycles is None or count < cycles:
            cycle_started = store.clocks.sample()[1]
            count += 1
            reasons: tuple[str, ...] = ()
            evidence_ids: tuple[str, ...] = ()
            proof_age = None
            time_proof_age = None
            guardian_receipt = None
            waiting = False
            cycle_failed = False
            observed.submission_requests = observed.acknowledgements = observed.confirmed_fills = 0
            observed.dispatch_failed = False
            try:
                refresh = getattr(broker, "refresh_fixture", None)
                if refresh:
                    refresh(store)
                if role == "guardian" and closed_poll.waiting(store):
                    reasons = (CLOSED_SESSION_IDLE,)
                elif role == "guardian":
                    if isinstance(broker, AlpacaCoreBroker):
                        deadline_wait = owned_deadline_wait(store)
                        if deadline_wait is not None and deadline_wait <= 0:
                            pass  # Deadline sends take priority over discretionary bar reads.
                        elif store.parallel_entries:
                            try:
                                async with asyncio.timeout(5):
                                    await broker.refresh_marks(owned_open_symbols(store))
                            except Exception:
                                pass  # Deadline/ownership recovery remains scheduled.
                        else:
                            for symbol in owned_open_symbols(store):
                                try:
                                    async with asyncio.timeout(5):
                                        await broker.refresh_mark(symbol)
                                except Exception:
                                    pass
                    receipt = await guardian_cycle(store, broker, now=store.clocks.sample()[0])
                    guardian_receipt = acknowledge_handoffs(store, now=store.clocks.sample()[0]).id
                    reasons, evidence_ids = receipt.reasons, (receipt.id,)
                    with suppress(ValueError, AssertionError):
                        closed_poll.observe(store, broker, receipt)
                elif role == "outcomes":
                    # Durable outcomes run before provider recovery and before any fit work.
                    batch = process_outcomes(store, cutoff=store.clocks.sample()[0])
                    evidence_ids = tuple(o.id for o in batch.outcomes)
                    reasons = batch.reasons
                    if learning_task is not None and learning_task.done():
                        try:
                            reasons += learning_task.result()
                        except Exception as error:
                            reasons += ("LEARNING_RETRY:" + type(error).__name__,)
                        learning_task = None
                    # Complete any existing learning job before starting capture.
                    # Canonical outcomes above keep progressing; independent Guardian
                    # and protection roles retain their original schedules and budgets.
                    if close_capture is not None and learning_task is None:
                        close_capture.poll()
                    close_provider_lane = (
                        close_capture is not None and close_capture.needs_provider_lane()
                    )
                    if close_provider_lane:
                        reasons += ("PAPER_CLOSE_CAPTURE_PENDING",)
                    elif _guardian_closed_idle(store):
                        reasons = (CLOSED_SESSION_IDLE, *reasons)
                    else:
                        try:
                            async with asyncio.timeout(5):
                                recovery = await recover_intents(store, broker)
                            reasons += recovery.reasons
                        except Exception as error:
                            reasons = ("OUTCOME_PROVIDER_RETRY:" + type(error).__name__,)
                    if learning_task is None and not close_provider_lane:
                        # The learner's immutable run identity includes eligibility
                        # exclusions, which can change without new outcome revisions.
                        learning_task = asyncio.create_task(
                            daily_consideration(store, broker, config)
                        )
                else:
                    boundary = int(store.clocks.sample()[0].timestamp() // 300)
                    waiting = boundary == last_boundary
                    decision = None
                    if boundary != last_boundary:
                        pending_candidate = None
                        pending_batch = ()
                        # Allow the cohort's bounded read plus clock/calendar and asset checks.
                        # Original 120-second observation and final-send freshness still apply.
                        async with asyncio.timeout(10):
                            observation = (
                                await broker.observation_cohort()
                                if store.config.selection_policy == "SIGNAL_OR_EXPLORE_V1"
                                else await broker.observation()
                            )
                        from quantbot.paper_core.contracts import ObservationCohort
                        from quantbot.paper_core.selection import cohort_document

                        observation_ids = (
                            (content_id(cohort_document(store, observation)),)
                            if isinstance(observation, ObservationCohort)
                            else (observation.id,)
                        )
                        with suppress(Exception):
                            observed.evaluate(
                                observation,
                                (),
                                evidence_ids=observation_ids,
                                now=store.clocks.sample()[0],
                            )
                        model = activation.read_active_model(store)

                        if store.parallel_entries:
                            decisions = await trader_batch_cycle(
                                store, broker, observation, model, now=store.clocks.sample()[0]
                            )
                            evidence_ids = tuple(d.id for d in decisions)
                            pending_batch = decisions
                            decision = decisions[0] if decisions else None
                        else:
                            decision = await trader_cycle(
                                store,
                                broker,
                                observation,
                                model,
                                store.latest_proof(),
                                now=store.clocks.sample()[0],
                            )
                            evidence_ids = (decision.id,)
                            pending_candidate = decision if decision.action == "BUY" else None
                        with suppress(Exception):
                            observed.evaluate(
                                observation,
                                decisions if store.parallel_entries else (decision,),
                                evidence_ids=(*observation_ids, *evidence_ids),
                                now=store.clocks.sample()[0],
                            )
                        # A provider can publish a boundary's bars a few seconds
                        # late. Empty cohorts have made no decision or reservation;
                        # retry them at the normal bounded role cadence.
                        if (
                            not isinstance(observation, ObservationCohort)
                            or observation.observations
                        ) and not _retry_transient_wait(
                            decisions if store.parallel_entries else (decision,),
                            source_end=observation.source_end,
                            now=store.clocks.sample()[0],
                        ):
                            last_boundary = boundary
                    pending_batch = retry_batch_candidates(store, pending_batch)
                    pending_candidate = retry_entry_candidate(store, pending_candidate)
                    send_reasons = await dispatch_entries(store, broker, observed=observed)
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
                if CLOSED_SESSION_IDLE not in reasons:
                    last_progress = store.clocks.sample()[0]
                failures = 0
            except Exception as error:
                cycle_failed = True
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
            if role == "trader":
                with suppress(Exception):
                    emit_cycle(
                        observed.snapshot(
                            cycle_id=content_id((heartbeat.id, count)),
                            now=now,
                            waiting=waiting,
                            reasons=reasons,
                            failure=cycle_failed,
                        )
                    )
            try:
                idle = CLOSED_SESSION_IDLE in reasons and guardian_receipt is None
                same_idle = bool(
                    idle
                    and idle_previous
                    and (
                        heartbeat.objections == idle_previous.objections
                        and heartbeat.evidence_ids == idle_previous.evidence_ids
                    )
                )
                replace_idle_id = (
                    idle_previous.id if same_idle and idle_previous.id != idle_anchor else None
                )
                publish_heartbeat(store, heartbeat, replace_idle_id=replace_idle_id)
                if not same_idle:
                    idle_anchor = heartbeat.id if idle else None
                idle_previous = heartbeat if idle else None
            except OperationalError as error:
                code = getattr(error.orig, "sqlite_errorcode", 0)
                if not isinstance(error.orig, sqlite3.OperationalError) or code & 255 not in {
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                }:
                    raise
                # Never print/publish an uncommitted heartbeat or retry its old time.
                # The next bounded role cycle must re-evaluate actual work/readiness.
                failures += 1
                print(
                    json.dumps({"role": role, "event": "HEARTBEAT_PUBLICATION_BUSY", "code": code}),
                    flush=True,
                )
            else:
                print(heartbeat.model_dump_json(), flush=True)
            # Backoff is per role; outcome polling has priority over fitting.
            delay = period if not failures else max(period, min(15, 2 ** min(failures - 1, 4)))
            if failures:
                delay = min(15, delay + random.uniform(0, min(0.25, delay * 0.1)))
            if cycles is None or count < cycles:
                remaining = max(0.001, delay - (store.clocks.sample()[1] - cycle_started))
                if role == "guardian" and not failures:
                    try:
                        deadline_wait = owned_deadline_wait(store)
                        if deadline_wait is not None:
                            # No tight retry loop: shared broker budgets/cooldowns still win.
                            remaining = min(remaining, deadline_wait if deadline_wait > 0 else 5)
                    except Exception:
                        pass  # Advisory wake calculation cannot stop the protection loop.
                await asyncio.sleep(max(0.001, remaining))
    finally:
        if close_capture is not None:
            await close_capture.aclose()
        if discovery_task is not None:
            discovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await discovery_task
        if learning_task is not None:
            await learning_task
