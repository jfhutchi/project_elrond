"""Completion can advance through market/entry faults without invented fees."""

from datetime import timedelta
from time import monotonic

import pytest
from tests.fixtures.core_m2 import NOW, events, fee_events, prepare


@pytest.mark.asyncio
async def test_quantity_only_proof_protects_but_never_unlocks_cash_entry(tmp_path):
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import evaluate_entry, trader_cycle
    from quantbot.storage import StorageRepository

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        quantity_proof = store.latest_proof()
        assert quantity_proof.expected_cash is None
        assert not quantity_proof.cash_complete
        assert (
            "INVENTORY_UNCERTIFIED"
            in evaluate_entry(store.config, observation, model, quantity_proof).gates
        )
        with db.transaction() as session:
            snapshot = StorageRepository(session).get_latest_account_snapshot(
                store.config.account_id, certified_only=True
            )
            assert snapshot is None
        broker.now = NOW + timedelta(minutes=30)
        receipt = await guardian_cycle(store, broker, now=broker.now)
        assert receipt.exit_intent_ids and len(broker.posts) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_closed_market_missing_observation_and_manual_pause_do_not_delay_outcome(tmp_path):
    from sqlalchemy import delete

    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.outcomes import import_broker_events, process_outcomes
    from quantbot.paper_core.store import table
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.storage import StorageRepository

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.now = NOW + timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        import_broker_events(store, events(store, broker))
        import_broker_events(store, fee_events(store, broker))
        with db.transaction() as session:
            StorageRepository(session).set_kill_switch(
                True, reason="manual pause", updated_at=broker.now
            )
            # Core outcomes consume durable decisions/fills, never fresh entry observations.
            # Remove fresh role health while retaining the pre-send ownership handoff.
            receipts = store.documents("core_guardian_receipts")
            for receipt in receipts:
                if receipt["recorded_at_utc"] != NOW.isoformat():
                    from quantbot.research.manifest import content_id

                    session.execute(
                        delete(table("core_guardian_receipts")).where(
                            table("core_guardian_receipts").c.id == content_id(receipt)
                        )
                    )
        broker.now = NOW + timedelta(hours=12)  # Closed session; no trader call.
        assert len(broker.posts) == 2
        import json
        import subprocess
        import sys
        from pathlib import Path

        worker = Path(__file__).parents[2] / "fixtures" / "core_restart_worker.py"
        # The independent closed-session worker creates the complete revision.
        process_started = monotonic()
        result = subprocess.run(
            [sys.executable, str(worker), str(tmp_path), "outcome_poll"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert monotonic() - process_started < 15
        polled = json.loads(result.stdout.split("M2_POLL:")[1])
        assert polled["elapsed_seconds"] < 15
        assert len(polled["outcome_ids"]) == 1
        assert polled["current"][0]["status"] == "COMPLETE"
        assert not process_outcomes(store, cutoff=broker.now).outcomes
        print("M2_POLL:" + json.dumps(polled, sort_keys=True))
        with db.transaction() as session:
            assert StorageRepository(session).get_kill_switch_state().engaged
    finally:
        db.close()


def test_empty_fills_cannot_become_round_trip(tmp_path):
    from quantbot.paper_core.outcomes import process_outcomes

    db, store, *_ = prepare(tmp_path)
    try:
        assert not process_outcomes(store, cutoff=NOW).outcomes
    finally:
        db.close()


@pytest.mark.asyncio
async def test_outcome_cannot_invent_missing_presend_guardian_handoff(tmp_path):
    from sqlalchemy import delete

    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.outcomes import OutcomeIncomplete, reconcile_round_trip
    from quantbot.paper_core.store import table
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        decision = await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.now = NOW + timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        with db.transaction() as session:
            session.execute(delete(table("core_guardian_receipts")))
        with pytest.raises(OutcomeIncomplete, match="HANDOFF"):
            reconcile_round_trip(store, decision.id, as_of=broker.now)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_independent_cash_activity_restores_entry_while_costs_remain_pending(tmp_path):
    from decimal import Decimal

    from quantbot.paper_core.contracts import BrokerEvent, CashActivity
    from quantbot.paper_core.guardian import guardian_cycle, synchronize
    from quantbot.paper_core.outcomes import import_broker_events, process_outcomes
    from quantbot.paper_core.trader import evaluate_entry, trader_cycle
    from quantbot.research.manifest import content_id
    from quantbot.storage import StateConflictError

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.now = NOW + timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        movements = tuple(
            (f.fill_id, f.price * f.quantity * (-1 if f.side.value == "BUY" else 1))
            for f in broker.fills
        )
        activity = CashActivity(
            recorded_at_utc=broker.now,
            account_id=store.config.account_id,
            document_id="independent-provider-cash-ledger",
            activity_ids=("cash-buy", "cash-sell"),
            movements=movements,
            complete=True,
            watermark=broker.now,
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
        incomplete = activity.model_copy(update={"complete": False})
        with pytest.raises(StateConflictError):
            import_broker_events(store, (event.model_copy(update={"cash_activity": incomplete}),))
        import_broker_events(store, (event,))
        await synchronize(store, broker, now=broker.now)
        cash_proof = store.latest_proof()
        assert cash_proof.cash_complete and cash_proof.expected_cash == Decimal(100000)
        fresh = observation.model_copy(
            update={
                "recorded_at_utc": broker.now,
                "source_end": broker.now,
                "available_at": broker.now,
                "sequence": 2,
            }
        )
        assert evaluate_entry(store.config, fresh, model, cash_proof).action == "BUY"
        process_outcomes(store, cutoff=broker.now)
        assert store.documents("core_round_trips")[0]["status"] == "COSTS_PENDING"
    finally:
        db.close()
