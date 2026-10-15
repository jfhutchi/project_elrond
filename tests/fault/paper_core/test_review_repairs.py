"""Synthetic regressions for M2 review: liveness, cash and incident scope."""

import asyncio
import json
import subprocess
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.fixtures.core_m2 import NOW, events, fee_events, prepare

from quantbot.domain import IntentState
from quantbot.paper_core.contracts import ProtectionPlan
from quantbot.paper_core.guardian import guardian_cycle, synchronize
from quantbot.paper_core.outcomes import (
    import_broker_events,
    process_outcomes,
    reconcile_round_trip,
    recover_intents,
)
from quantbot.paper_core.runtime import coordinator
from quantbot.paper_core.send_guard import definitively_unsent
from quantbot.paper_core.store import table
from quantbot.paper_core.trader import claim_entry, evaluate_entry, record_observation, trader_cycle
from quantbot.risk import DrawdownState, OrderPurpose, PortfolioVetoDecision
from quantbot.storage import StateConflictError, StorageRepository


@pytest.mark.parametrize("cut", ["after_claim", "after_order_created"])
def test_fresh_process_stale_claim_recovery_allows_exactly_one_fresh_post(tmp_path, cut):
    db, *_ = prepare(tmp_path)
    db.close()
    fixtures = Path(__file__).parents[2] / "fixtures"
    first = subprocess.run(
        [sys.executable, str(fixtures / "core_restart_worker.py"), str(tmp_path), cut],
        capture_output=True,
        text=True,
    )
    assert first.returncode == 91, first.stdout + first.stderr
    results = []
    for _ in range(2):
        resumed = subprocess.run(
            [sys.executable, str(fixtures / "core_unsent_recovery_worker.py"), str(tmp_path)],
            capture_output=True,
            text=True,
        )
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        results.append(json.loads(resumed.stdout.split("M2_FRESH_UNSENT:")[1]))
    assert results[0] == results[1]
    result = results[0]
    assert result["old_state"] == "ERROR"
    assert result["post_count"] == 1 and result["post_ids"][0] != result["old_intent"]
    assert result["links"] == 2
    assert len(result["refusal"]) == 1
    assert result["refusal"][0]["definitively_unsent"] is True
    assert result["old_decision"] == result["replayed_decision"]
    print(
        "M2_REPAIR:"
        + json.dumps({"case": "fresh_process_unsent", "cut": cut, **result}, sort_keys=True)
    )


def fresh_observation(observation, now, sequence=2):
    return observation.model_copy(
        update={
            "recorded_at_utc": now,
            "available_at": now,
            "source_start": now - timedelta(minutes=5),
            "source_end": now,
            "dependency_available_at": (now,),
            "sequence": sequence,
            "raw_data_digest": f"fresh-{sequence}",
        }
    )


def send_arguments():
    return dict(
        purpose=OrderPurpose.ENTRY,
        market_eligible=True,
        risk_approved=True,
        drawdown=DrawdownState(
            current_equity=100000,
            high_water_equity=100000,
            drawdown_fraction=0,
            new_risk_multiplier=1,
            entry_halted=False,
            liquidation_required=False,
        ),
        vetoes=PortfolioVetoDecision(
            symbol="XLE",
            policy_version=1,
            rule_parameters_hash="EXPLICIT_SYNTHETIC_ASSUMPTIONS",
            evaluated=(),
            rejections=(),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [IntentState.RISK_APPROVED, IntentState.ORDER_CREATED])
async def test_stale_unsent_recovery_frees_fresh_claim_and_fences_delayed_sender(tmp_path, state):
    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        record_observation(store, observation)
        decision = evaluate_entry(store.config, observation, model, proof)
        entry = claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
        if state is IntentState.ORDER_CREATED:
            with db.transaction() as session:
                entry = entry.model_copy(
                    update={
                        "intent": StorageRepository(session).transition_order_intent(
                            entry.intent.intent_id,
                            state,
                            expected_current=IntentState.RISK_APPROVED,
                        )
                    }
                )
        waiting, resume = asyncio.Event(), asyncio.Event()
        original = broker.get_account
        old_task = None

        async def delayed():
            result = await original()
            if asyncio.current_task() is old_task:
                waiting.set()
                await resume.wait()
            return result

        broker.get_account = delayed
        old_task = asyncio.create_task(
            coordinator(store, broker).submit_durable(
                entry.intent,
                **send_arguments(),
            )
        )
        await waiting.wait()
        broker.now = decision.valid_until
        await recover_intents(store, broker)
        with db.transaction() as session:
            repo = StorageRepository(session)
            assert definitively_unsent(repo, entry.intent.intent_id)
            refusal = [
                e for e in repo.list_order_events() if e.event_type == "CORE_ENTRY_SEND_REFUSED"
            ]
            assert len(refusal) == 1
        await recover_intents(store, broker)
        await guardian_cycle(store, broker, now=broker.now)
        fresh = fresh_observation(observation, broker.now)
        await trader_cycle(store, broker, fresh, model, store.latest_proof(), now=broker.now)
        assert len(broker.posts) == 1 and broker.posts[0].intent_id != entry.intent.intent_id
        resume.set()
        result = (await asyncio.gather(old_task, return_exceptions=True))[0]
        assert isinstance(result, Exception)
        assert len(broker.posts) == 1
        print(
            "M2_REPAIR:"
            + json.dumps(
                {
                    "case": "delayed_sender",
                    "cut": state.value,
                    "refused_intent": entry.intent.intent_id,
                    "decision_id": decision.id,
                    "new_post": broker.posts[0].intent_id,
                    "post_count": len(broker.posts),
                    "old_sender_error": type(result).__name__,
                    "refusal": refusal[0].detail,
                },
                sort_keys=True,
            )
        )
        assert (
            await trader_cycle(store, broker, observation, model, proof, now=broker.now)
        ).id == decision.id
        assert len(broker.posts) == 1
    finally:
        if old_task is not None and not old_task.done():
            old_task.cancel()
            await asyncio.gather(old_task, return_exceptions=True)
        db.close()


@pytest.mark.asyncio
async def test_late_cost_revokes_both_cash_readers_and_prevents_fresh_post(tmp_path):
    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.now += timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        zero = fee_events(store, broker)
        import_broker_events(store, zero)
        await synchronize(store, broker, now=broker.now)
        old = store.latest_proof()
        revisions = fee_events(
            store,
            broker,
            amount=".10",
            revision=2,
            supersedes={e.cost.fill_id: e.cost.id for e in zero},
        )
        import_broker_events(store, revisions)
        broker.cash -= Decimal(".20")
        with db.transaction() as session:
            assert store.ledger(session)[1] == Decimal("99999.8")
            assert (
                StorageRepository(session).get_latest_account_snapshot(
                    store.config.account_id, certified_only=True
                )
                is None
            )
        with pytest.raises(StateConflictError, match="CASH_AUTHORITY"):
            store.latest_proof()
        fresh = fresh_observation(observation, broker.now)
        await trader_cycle(store, broker, fresh, model, old, now=broker.now)
        assert len(broker.posts) == 2
        await synchronize(store, broker, now=broker.now)
        recovered = store.latest_proof()
        broker.now += timedelta(seconds=1)
        await trader_cycle(
            store,
            broker,
            fresh_observation(observation, broker.now, 3),
            model,
            recovered,
            now=broker.now,
        )
        assert len(broker.posts) == 3
        with db.transaction() as session:
            revocations = [
                dict(r)
                for r in session.execute(
                    select(table("core_cash_authority_revocations"))
                ).mappings()
            ]
        print(
            "M2_REPAIR:"
            + json.dumps(
                {
                    "case": "late_cash",
                    "old_proof": old.id,
                    "old_cash": str(old.expected_cash),
                    "new_cash": "99999.8",
                    "recovered_proof": recovered.id,
                    "recovered_cash": str(recovered.expected_cash),
                    "post_count_before_recovery": 2,
                    "post_count_after_recovery": len(broker.posts),
                    "revocations": revocations,
                    "costs": store.documents("core_cost_items"),
                }
            )
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_conflicting_batch_only_quarantines_implicated_lot_and_rolls_back_neighbors(tmp_path):
    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        first = await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.now += timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        import_broker_events(store, fee_events(store, broker))
        await synchronize(store, broker, now=broker.now)
        broker.now += timedelta(seconds=1)
        second = await trader_cycle(
            store,
            broker,
            fresh_observation(observation, broker.now),
            model,
            store.latest_proof(),
            now=broker.now,
        )
        broker.now += timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        known = {e["fill_id"] for e in store.documents("core_cost_items")}
        import_broker_events(
            store, tuple(e for e in fee_events(store, broker) if e.cost.fill_id not in known)
        )
        process_outcomes(store, cutoff=broker.now)
        batch = list(events(store, broker))
        bad = batch[1].model_copy(update={"raw_digest": "conflicting-one-old-fill"})
        batch[1] = bad
        # This healthy source row is processed before the bad fill, then rolled back.
        healthy_order = next(
            e for e in batch if e.order and e.client_order_id == broker.posts[2].client_order_id
        )
        batch.insert(0, healthy_order.model_copy(update={"event_id": "healthy-new-source-receipt"}))
        before = store.documents("core_broker_event_sources")
        with pytest.raises(StateConflictError):
            import_broker_events(store, tuple(batch))
        assert store.documents("core_broker_event_sources") == before
        with db.transaction() as session:
            rows = {
                r["lot_id"]: r
                for r in session.execute(select(table("core_outcome_index"))).mappings()
            }
            assert rows[first.id]["status"] == "INTEGRITY_FAILURE"
            assert rows[second.id]["status"] == "COMPLETE"
            incident = StorageRepository(session).list_incidents(unresolved_only=True)[-1]
            assert incident.detail["lot_ids"] == [first.id]
            assert len(incident.detail["source_events"]) == len(batch)
        result = process_outcomes(store, cutoff=broker.now)
        assert len(result.reasons) == 1
        assert reconcile_round_trip(store, second.id, as_of=broker.now).status == "COMPLETE"
        print(
            "M2_REPAIR:"
            + json.dumps(
                {
                    "case": "bounded_quarantine",
                    "bad_lot": first.id,
                    "healthy_lot": second.id,
                    "incident": incident.detail,
                    "reasons": result.reasons,
                },
                sort_keys=True,
            )
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_independent_cash_survives_fee_allocation_revision_and_allows_post(tmp_path):
    from quantbot.paper_core.contracts import BrokerEvent, CashActivity
    from quantbot.research.manifest import content_id

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        first = await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.now += timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        activity = CashActivity(
            recorded_at_utc=broker.now,
            account_id=store.config.account_id,
            document_id="synthetic-complete-independent-cash",
            activity_ids=("cash-buy", "cash-sell"),
            complete=True,
            watermark=broker.now,
            movements=tuple(
                (f.fill_id, (-1 if f.side.value == "BUY" else 1) * f.quantity * f.price)
                for f in broker.fills
            ),
        )
        event = BrokerEvent(
            recorded_at_utc=broker.now,
            occurred_at=broker.now,
            account_id=store.config.account_id,
            endpoint="synthetic/cash-activities",
            event_id=activity.id,
            raw_digest=content_id(activity),
            client_order_id=broker.posts[0].client_order_id,
            cash_activity=activity,
        )
        import_broker_events(store, (event,))
        await synchronize(store, broker, now=broker.now)
        full = store.latest_proof()
        assert reconcile_round_trip(store, first.id, as_of=broker.now).status == "COSTS_PENDING"
        zero = fee_events(store, broker)
        import_broker_events(store, zero)
        broker.now += timedelta(seconds=1)
        import_broker_events(
            store,
            fee_events(
                store,
                broker,
                amount=".10",
                revision=2,
                supersedes={e.cost.fill_id: e.cost.id for e in zero},
            ),
        )
        assert store.latest_proof().id == full.id
        with db.transaction() as session:
            assert (
                StorageRepository(session)
                .get_latest_account_snapshot(store.config.account_id, certified_only=True)
                .account.cash
                == 100000
            )
        assert reconcile_round_trip(store, first.id, as_of=broker.now).net_pnl == Decimal("-.2")
        await trader_cycle(
            store, broker, fresh_observation(observation, broker.now), model, full, now=broker.now
        )
        assert len(broker.posts) == 3
        print(
            "M2_REPAIR:"
            + json.dumps(
                {
                    "case": "independent_cash",
                    "cash_proof": full.id,
                    "cash": str(full.expected_cash),
                    "allocated_net": "-.2",
                    "post_count": 3,
                }
            )
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cash_guard_and_send_cas_exclude_concurrent_process_cost_import(tmp_path):
    from quantbot.paper_core.guardian import acknowledge_handoffs

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.now += timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        zero = fee_events(store, broker)
        import_broker_events(store, zero)
        await synchronize(store, broker, now=broker.now)
        broker.now += timedelta(seconds=1)
        revisions = fee_events(
            store,
            broker,
            amount=".10",
            revision=2,
            supersedes={e.cost.fill_id: e.cost.id for e in zero},
        )
        event_file = tmp_path / "cost-events.json"
        event_file.write_text(json.dumps([e.model_dump(mode="json") for e in revisions]))
        fresh = fresh_observation(observation, broker.now)
        record_observation(store, fresh)
        decision = evaluate_entry(store.config, fresh, model, store.latest_proof())
        entry = claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
        acknowledge_handoffs(store, now=broker.now)
        sender = coordinator(store, broker)
        check = sender._transactional_send_check
        witness = []

        def check_and_contend(session, intent, purpose):
            objections = check(session, intent, purpose)
            assert not objections
            code = """
import json,sys
from quantbot.storage import Database
from quantbot.paper_core.store import CoreStore
from quantbot.paper_core.contracts import CoreConfig,BrokerEvent
from quantbot.paper_core.outcomes import import_broker_events
from sqlalchemy.exc import OperationalError
with Database(sys.argv[1],busy_timeout_ms=50) as db:
    store=CoreStore(db,CoreConfig(account_id='synthetic-account',symbol='XLE'))
    try:
        events=tuple(BrokerEvent.model_validate(e) for e in json.load(open(sys.argv[2])))
        import_broker_events(store,events)
    except OperationalError as error:
        assert 'locked' in str(error)
        print('COST_IMPORT_LOCKED_BEFORE_SEND_CAS')
    else:
        raise AssertionError('cost import crossed locked guard/send boundary')
"""
            process = subprocess.run(
                [sys.executable, "-c", code, str(db.path), str(event_file)],
                capture_output=True,
                text=True,
            )
            assert process.returncode == 0, process.stdout + process.stderr
            witness.append(process.stdout.strip())
            return objections

        sender._transactional_send_check = check_and_contend
        original_submit = broker.submit_order

        async def at_transport(intent):
            with db.transaction() as session:
                assert (
                    StorageRepository(session).get_order_intent(intent.intent_id).state
                    is IntentState.SUBMITTING
                )
            import_broker_events(store, revisions)
            with pytest.raises(StateConflictError, match="CASH_AUTHORITY"):
                store.latest_proof()
            return await original_submit(intent)

        broker.submit_order = at_transport
        await sender.submit_durable(entry.intent, **send_arguments())
        assert len(broker.posts) == 3
        assert len(store.documents("core_cost_items")) == 4
        print(
            "M2_REPAIR:"
            + json.dumps(
                {
                    "case": "cash_send_writer_race",
                    "witness": witness,
                    "winner": "SUBMITTING_CAS_BEFORE_COST_IMPORT",
                    "post_count": 3,
                }
            )
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cost_import_wins_during_last_acquisition_and_refuses_prepared_send(tmp_path):
    from quantbot.execution import OrderSubmissionBlocked

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.now += timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        zero = fee_events(store, broker)
        import_broker_events(store, zero)
        await synchronize(store, broker, now=broker.now)
        full = store.latest_proof()
        broker.now += timedelta(seconds=1)
        original_positions = broker.get_positions
        calls = 0

        async def revise_at_last_acquisition():
            nonlocal calls
            result = await original_positions()
            calls += 1
            if calls == 2:
                import_broker_events(
                    store,
                    fee_events(
                        store,
                        broker,
                        amount=".10",
                        revision=2,
                        supersedes={e.cost.fill_id: e.cost.id for e in zero},
                    ),
                )
                broker.cash -= Decimal(".20")
            return result

        broker.get_positions = revise_at_last_acquisition
        with pytest.raises(OrderSubmissionBlocked) as error:
            await trader_cycle(
                store,
                broker,
                fresh_observation(observation, broker.now),
                model,
                full,
                now=broker.now,
            )
        assert "CORE_CASH_AUTHORITY_UNAVAILABLE" in error.value.decision.reasons
        assert len(broker.posts) == 2
        with db.transaction() as session:
            old = store.links()[-1]
            assert definitively_unsent(StorageRepository(session), old["intent_id"])
        print(
            "M2_REPAIR:"
            + json.dumps(
                {
                    "case": "cost_import_wins",
                    "post_count": 2,
                    "objections": error.value.decision.reasons,
                    "refused_intent": old["intent_id"],
                }
            )
        )
    finally:
        db.close()
