"""Fresh-process crash cuts use the same provider client IDs and durable journal."""

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from tests.fixtures.core_m2 import NOW, JournalBroker, prepare


@pytest.mark.parametrize(
    "cut",
    [
        "before_claim",
        "after_claim",
        "after_order_created",
        "after_send",
        "fill_import",
        "before_outcome",
        "after_outcome",
    ],
)
def test_fresh_process_restart_cut_never_reposts_or_duplicates(tmp_path, cut):
    worker = Path(__file__).parents[2] / "fixtures" / "core_restart_worker.py"
    db, _, _, _, _, _ = prepare(tmp_path)
    db.close()
    first = subprocess.run(
        [sys.executable, str(worker), str(tmp_path), cut], text=True, capture_output=True
    )
    assert first.returncode == 91, first.stdout + first.stderr
    resumed = subprocess.run(
        [sys.executable, str(worker), str(tmp_path), "recover"], text=True, capture_output=True
    )
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    one = json.loads(resumed.stdout.split("M2_RECOVERY:")[1])
    again = subprocess.run(
        [sys.executable, str(worker), str(tmp_path), "recover"], text=True, capture_output=True
    )
    assert again.returncode == 0, again.stdout + again.stderr
    two = json.loads(again.stdout.split("M2_RECOVERY:")[1])
    assert one == two
    if cut in {"before_claim", "after_claim", "after_order_created"}:
        assert one["posts"] == 0 and not one["current"]
    else:
        assert one["posts"] == 2 and len(one["current"]) == 1
        assert one["current"][0]["net_pnl"] == "0"
        assert one["current"][0]["open_quantity"] == "0"
        assert one["quantities"] == {"SPY": "2", "XLE": "0"}
        assert one["cash"] == "100000"
    print("M2_RESTART:" + json.dumps({"cut": cut, **one}, sort_keys=True))
    assert len(JournalBroker(tmp_path / "broker.json").posts) == one["posts"]


@pytest.mark.asyncio
async def test_404_after_possible_post_stays_uncertain(tmp_path):
    from quantbot.domain import IntentState
    from quantbot.paper_core.contracts import ProtectionPlan
    from quantbot.paper_core.outcomes import recover_intents
    from quantbot.paper_core.trader import claim_entry, evaluate_entry, record_observation
    from quantbot.storage import StorageRepository

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        record_observation(store, observation)
        decision = evaluate_entry(store.config, observation, model, proof)
        entry = claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
        with db.transaction() as session:
            repo = StorageRepository(session)
            repo.transition_order_intent(
                entry.intent.intent_id,
                IntentState.ORDER_CREATED,
                expected_current=IntentState.RISK_APPROVED,
            )
            repo.transition_order_intent(
                entry.intent.intent_id,
                IntentState.SUBMITTING,
                expected_current=IntentState.ORDER_CREATED,
            )
        await recover_intents(store, broker)
        await recover_intents(store, broker)
        with db.transaction() as session:
            assert (
                StorageRepository(session).get_order_intent(entry.intent.intent_id).state
                is IntentState.SUBMISSION_UNKNOWN
            )
        assert not broker.posts
    finally:
        db.close()


@pytest.mark.asyncio
async def test_partial_fill_is_protected_before_entry_terminal(tmp_path):
    from decimal import Decimal

    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        broker.partial = Decimal(2)
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.partial = None
        broker.now = NOW + timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        assert len(broker.posts) == 2 and broker.posts[-1].quantity == Decimal(2)
        assert not store.documents("core_round_trips")  # Entry still has possible exposure.
        await guardian_cycle(store, broker, now=broker.now)
        assert len(broker.posts) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cancel_ack_and_late_fill_keep_reservation_until_complete_watermark(tmp_path):
    from decimal import Decimal

    from quantbot.domain import Fill, OrderSide
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.partial = Decimal(2)
        broker.now = NOW + timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        assert len(broker.posts) == 2
        exit_order = broker.orders[broker.posts[-1].client_order_id]
        broker.partial = None
        broker.orders[exit_order.client_order_id] = exit_order.model_copy(
            update={"status": "canceled"}
        )

        async def incomplete_watermark():
            return None

        broker.get_activity_watermark = incomplete_watermark
        broker.now += timedelta(seconds=1)
        await guardian_cycle(store, broker, now=broker.now)
        assert len(broker.posts) == 2, (
            "cancel ack alone cannot release potentially filling quantity"
        )
        broker.fills.append(
            Fill(
                fill_id="late-cancel-fill",
                broker_order_id=exit_order.broker_order_id,
                symbol="XLE",
                side=OrderSide.SELL,
                quantity=Decimal(1),
                price=Decimal(100),
                occurred_at=broker.now,
            )
        )
        broker.orders[exit_order.client_order_id] = exit_order.model_copy(
            update={"status": "canceled", "filled_quantity": Decimal(3)}
        )
        broker.quantities["XLE"] -= 1
        broker.cash += 100
        await guardian_cycle(store, broker, now=broker.now)
        assert len(broker.posts) == 2

        async def complete_watermark():
            return broker.now

        broker.get_activity_watermark = complete_watermark
        await guardian_cycle(store, broker, now=broker.now)
        assert len(broker.posts) == 3 and broker.posts[-1].quantity == Decimal(2)
        assert sum(
            (f.quantity for f in broker.fills if f.side is OrderSide.SELL), Decimal(0)
        ) == Decimal(5)
        assert broker.quantities["SPY"] == Decimal(2)
        assert len(store.documents("core_round_trips")) == 1
        print(
            "M2_RESERVATION:" + json.dumps(store.documents("core_round_trips")[0], sort_keys=True)
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_multiple_entry_partials_each_get_owned_exit_and_conserve_money(tmp_path):
    from decimal import Decimal

    from tests.fixtures.core_m2 import fee_events

    from quantbot.domain import Fill, OrderSide
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.outcomes import import_broker_events, process_outcomes
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        broker.partial = Decimal(2)
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.partial = None
        broker.now = NOW + timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        assert broker.posts[-1].quantity == Decimal(2)
        broker.now += timedelta(seconds=10)
        order = broker.orders[broker.posts[0].client_order_id]
        broker.fills.append(
            Fill(
                fill_id="entry-partial-2",
                broker_order_id=order.broker_order_id,
                symbol="XLE",
                side=OrderSide.BUY,
                quantity=3,
                price=100,
                occurred_at=broker.now,
            )
        )
        broker.orders[order.client_order_id] = order.model_copy(
            update={"status": "filled", "filled_quantity": Decimal(5)}
        )
        broker.quantities["XLE"] += 3
        broker.cash -= 300
        broker.now += timedelta(seconds=1)
        await guardian_cycle(store, broker, now=broker.now)
        assert len(broker.posts) == 3 and broker.posts[-1].quantity == Decimal(3)
        assert broker.cash == Decimal(100000) and broker.quantities["XLE"] == 0
        assert broker.quantities["SPY"] == 2
        import_broker_events(store, fee_events(store, broker, amount=".10"))
        trip = process_outcomes(store, cutoff=broker.now).outcomes[0]
        assert len(trip.entry_fill_ids) == len(trip.exit_fill_ids) == 2
        assert trip.gross_pnl == 0 and trip.net_pnl == Decimal("-.4")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_concurrent_stop_deadline_contenders_cannot_double_sell(tmp_path):
    import asyncio
    from decimal import Decimal

    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.store import CoreStore
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.storage import Database

    db, store, broker, proof, observation, model = prepare(tmp_path)
    peer_db = None
    try:
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        peer_db = Database(db.path)
        peer = CoreStore(peer_db, store.config)
        broker.now = NOW + timedelta(minutes=30)
        broker.set_mark("XLE", Decimal(98), as_of=broker.now)
        original = broker.get_account

        async def interleave_acquisition():
            await asyncio.sleep(0)
            return await original()

        broker.get_account = interleave_acquisition
        await asyncio.gather(
            guardian_cycle(store, broker, now=broker.now),
            guardian_cycle(peer, broker, now=broker.now),
            return_exceptions=True,
        )
        await guardian_cycle(store, broker, now=broker.now)
        assert len(broker.posts) == 2 and broker.posts[-1].quantity == Decimal(5)
        assert broker.quantities["XLE"] == 0 and broker.quantities["SPY"] == 2
        assert len(store.documents("core_round_trips")) == 1
        print(
            "M2_RESERVATION:" + json.dumps(store.documents("core_round_trips")[0], sort_keys=True)
        )
    finally:
        if peer_db is not None:
            peer_db.close()
        db.close()
