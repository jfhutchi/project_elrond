"""Delayed owned fills never justify adoption, guessed costs, or blanket quarantine resets."""

import json
from datetime import datetime

import pytest
from sqlalchemy import select
from tests.integration.paper_core.test_daily_paper_loop import DailyProvider
from tests.integration.paper_core.test_paper_funds import close, enter, prepare, ready

from quantbot.paper_core.authority import commission_in_session, executable_in_session
from quantbot.paper_core.contracts import ReconciliationProof
from quantbot.paper_core.guardian import _pending_owned_activity, guardian_cycle, synchronize
from quantbot.paper_core.health import active_health, report
from quantbot.paper_core.protection_bridge import core_position_protection
from quantbot.paper_core.store import table
from quantbot.paper_core.time_evidence import acquire_time_evidence
from quantbot.storage import StorageRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["SUBMITTING", "SUBMISSION_UNKNOWN", "NO_LOCAL_ORDER"])
async def test_exact_pending_ack_is_retryable_during_submission(tmp_path, state):
    class Lag(DailyProvider):
        def __init__(self, clocks):
            super().__init__(clocks)
            self.activity_lag_seconds = 60

    db, store, broker, _, clocks = await prepare(tmp_path, provider_factory=Lag)
    try:
        await enter(store, broker)
        with pytest.raises(TimeoutError, match="OWNED_FILL_ACTIVITY_PENDING"):
            await synchronize(store, broker, now=clocks.wall)
        event = store.documents("core_health_events")[-1]
        proof = store.read(
            "core_reconciliation_proofs", event["evidence_ids"][0], ReconciliationProof
        )
        with db.transaction() as session:
            progress = session.execute(select(table("core_provider_progress"))).mappings().one()
            source = json.loads(
                session.execute(
                    select(table("core_provider_acquisitions").c.payload).where(
                        table("core_provider_acquisitions").c.id == progress["document_id"]
                    )
                ).scalar_one()
            )
            session.execute(
                table("order_intents")
                .update()
                .values(state="SUBMITTING" if state == "NO_LOCAL_ORDER" else state)
            )
            if state == "NO_LOCAL_ORDER":
                session.execute(table("broker_orders").delete())
        assert _pending_owned_activity(
            store, proof, capture_started=datetime.fromisoformat(source["until"]), now=clocks.wall
        )
        assert store.latest_proof().id != proof.id
        assert not any(
            h["state"] == "INTEGRITY_FAILURE" for h in store.documents("core_health_events")
        )
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "approval",
        "binding",
        "stale-proof",
        "unresolved",
        "basis",
        "other-integrity",
        "manual-pause",
        "source-missing",
        "ack-missing",
        "replay",
    ],
)
async def test_recovery_refuses_incomplete_or_conflicting_authority(tmp_path, fault):
    from quantbot.paper_core.health import record_health, resolve_delayed_owned_buy

    db, store, broker, provider, clocks, approval, decision = await delayed_owned_quarantine(
        tmp_path
    )
    try:
        if fault == "approval":
            approval["approval_reference"] = ""
        elif fault == "binding":
            approval["binding_id"] = "a-stale-executable"
        elif fault == "stale-proof":
            clocks.advance(31)
        elif fault == "unresolved":
            with db.transaction() as session:
                session.execute(
                    table("order_intents")
                    .update()
                    .where(table("order_intents").c.symbol == decision.symbol)
                    .values(state="SUBMISSION_UNKNOWN")
                )
        elif fault == "basis":
            proof = store.latest_proof()
            changed = proof.model_copy(
                update={
                    "positions": tuple(
                        p.model_copy(update={"average_entry_price": p.average_entry_price + 1})
                        if p.symbol == decision.symbol
                        else p
                        for p in proof.positions
                    )
                }
            )
            store.record("core_reconciliation_proofs", changed)
            with db.transaction() as session:
                session.execute(
                    table("core_quantity_certifications")
                    .update()
                    .where(table("core_quantity_certifications").c.proof_id == proof.id)
                    .values(proof_id=changed.id)
                )
            approval["proof_id"] = changed.id
        elif fault in {"other-integrity", "manual-pause"}:
            with db.transaction() as session:
                target = next(h for h in active_health(session) if h.id == approval["health_id"])
            record_health(
                store,
                target.model_copy(
                    update={
                        "state": "MANUAL_PAUSE" if fault == "manual-pause" else "INTEGRITY_FAILURE",
                        "objections": ("UNRELATED_EXPLICIT_BLOCK",),
                    }
                ),
            )
        elif fault == "source-missing":
            with db.transaction() as session:
                from sqlalchemy import func

                session.execute(
                    table("core_broker_event_sources")
                    .delete()
                    .where(
                        func.json_extract(
                            table("core_broker_event_sources").c.payload, "$.fill.symbol"
                        )
                        == decision.symbol
                    )
                )
        elif fault == "ack-missing":
            with db.transaction() as session:
                from sqlalchemy import func

                session.execute(
                    table("core_provider_acquisitions")
                    .delete()
                    .where(
                        func.json_extract(
                            table("core_provider_acquisitions").c.payload,
                            "$.pending_owned_orders[0].symbol",
                        )
                        == decision.symbol
                    )
                )
        elif fault == "replay":
            resolve_delayed_owned_buy(store, approval)
        before = store.documents("core_health_events")
        from quantbot.storage import StateConflictError

        with pytest.raises(StateConflictError, match="OWNED_BUY_RECOVERY_"):
            resolve_delayed_owned_buy(store, approval)
        assert store.documents("core_health_events") == before
        assert len(provider.orders) == 3
    finally:
        db.close()


@pytest.mark.asyncio
async def test_blocked_exit_is_visible_even_with_a_fresh_healthy_heartbeat(tmp_path):
    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        decision, _ = await enter(store, broker)
        await synchronize(store, broker, now=clocks.wall)
        ready(store)
        health_id = report(
            store,
            role="GUARDIAN",
            state="INTEGRITY_FAILURE",
            scope="SYMBOL",
            scope_id=decision.symbol,
            reasons=("INVENTORY_OWNERSHIP_CONTRADICTION",),
            now=clocks.wall,
            protection_blocked=True,
        )
        position = (await broker.get_positions())[0]
        with db.transaction() as session:
            reserved, delegated, _ = core_position_protection(
                session, account_id=store.config.account_id, position=position, now=clocks.wall
            )
        assert reserved and not delegated
        clocks.advance(1800)
        store.runtime_role = "guardian"
        receipt = await guardian_cycle(store, broker, now=clocks.wall)
        assert "OWNED_PROTECTION_QUARANTINED:" + health_id in receipt.reasons
        assert not receipt.exit_intent_ids and len(provider.orders) == 1
    finally:
        db.close()


async def delayed_owned_quarantine(tmp_path):
    db, store, broker, provider, clocks = await prepare(tmp_path, provider_factory=DailyProvider)
    await enter(store, broker)
    await close(store, broker, clocks)
    provider.activity_lag_seconds = 60
    decision, reasons = await enter(store, broker)
    assert not reasons
    with pytest.raises(TimeoutError, match="OWNED_FILL_ACTIVITY_PENDING"):
        await synchronize(store, broker, now=clocks.wall)
    pending = store.documents("core_health_events")[-1]
    failed = store.read(
        "core_reconciliation_proofs", pending["evidence_ids"][0], ReconciliationProof
    )
    assert not failed.cash_complete
    health_id = report(
        store,
        role="GUARDIAN",
        state="INTEGRITY_FAILURE",
        scope="SYMBOL",
        scope_id=decision.symbol,
        reasons=("INVENTORY_OWNERSHIP_CONTRADICTION",),
        now=clocks.wall,
        evidence_ids=(failed.id,),
        protection_blocked=True,
    )
    clocks.advance(60)
    await synchronize(store, broker, now=clocks.wall)
    time_proof = await acquire_time_evidence(broker, clocks)
    store.record("core_time_evidence", time_proof)
    ready(store, time_proof)
    with db.transaction() as session:
        binding = executable_in_session(session)
        commission = commission_in_session(store, session)
        # The retryable health event is separate from the exact integrity event being reviewed.
    now = clocks.wall
    approval = dict(
        health_id=health_id,
        failed_proof_id=failed.id,
        proof_id=store.latest_proof().id,
        binding_id=binding.id,
        commission_id=commission.id,
        runtime_release_id=store.runtime_release_id,
        approved_at=now.isoformat(),
        approval_reference="EXPLICIT_OFFLINE_RECOVERY_APPROVAL",
    )
    return db, store, broker, provider, clocks, approval, decision


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_paused", [False, True])
async def test_audited_recovery_preserves_originals_and_normal_guardian_exits(
    tmp_path, entry_paused
):
    from quantbot.paper_core import health

    resolver = getattr(health, "resolve_delayed_owned_buy", None)
    assert callable(resolver), "an explicit evidence-checked recovery API is required"
    db, store, broker, provider, clocks, approval, decision = await delayed_owned_quarantine(
        tmp_path
    )
    try:
        originals = {
            name: store.documents(name)
            for name in (
                "core_decisions",
                "core_protection_plans",
                "core_learning_runs",
                "core_models",
            )
        }
        with db.transaction() as session:
            if entry_paused:
                StorageRepository(session).set_kill_switch(
                    True, reason="EXPLICIT_ENTRY_PAUSE", updated_at=clocks.wall
                )
            stop = StorageRepository(session).get_kill_switch_state()
        receipt_id = resolver(store, approval)
        with db.transaction() as session:
            assert approval["health_id"] not in {h.id for h in active_health(session)}
            assert StorageRepository(session).get_kill_switch_state() == stop
            assert session.execute(
                select(table("core_provider_acquisitions").c.id).where(
                    table("core_provider_acquisitions").c.id == receipt_id
                )
            ).first()
        assert all(store.documents(name) == docs for name, docs in originals.items())
        assert len(provider.orders) == 3  # Resolver has no broker or send interface.
        clocks.advance(1800)
        store.runtime_role = "guardian"
        receipt = await guardian_cycle(store, broker, now=clocks.wall)
        assert receipt.exit_intent_ids
        assert provider.orders[-1]["symbol"] == decision.symbol
        assert provider.orders[-1]["side"] == "sell"
        assert provider.orders[-1]["qty"] == provider.orders[-2]["qty"]
        assert store.documents("core_round_trips")[-1]["actual_fees"] is None
    finally:
        db.close()
