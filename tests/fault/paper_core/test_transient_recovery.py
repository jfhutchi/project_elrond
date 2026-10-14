"""Ordinary acquisition faults retain evidence and require fresh scoped proof."""

from datetime import timedelta
from decimal import Decimal

import pytest
from tests.fixtures.core_m3 import fresh, prepare


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "changing_marks"])
async def test_acquisition_wait_does_not_latch_stop_and_fresh_proof_resumes(tmp_path, failure):
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.storage import StorageRepository

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    original = broker.get_account
    calls = 0

    async def unstable():
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise TimeoutError("synthetic provider timeout")
        value = await original()
        return value.model_copy(
            update={"account": value.account.model_copy(update={"equity": Decimal(100000 + calls)})}
        )

    broker.get_account = unstable
    try:
        decision = await trader_cycle(store, broker, observation, model, proof, now=clocks.wall)
        assert decision.action == "WAIT" and broker.posts == []
        with db.transaction() as session:
            assert not StorageRepository(session).get_kill_switch_state().engaged
        failed = store.documents("core_health_events")
        assert any(h["state"] == "RETRYABLE_FAILURE" for h in failed)
        clocks.advance(400)
        await trader_cycle(store, broker, fresh(observation, clocks), model, proof, now=clocks.wall)
        assert all(h["state"] != "INTEGRITY_FAILURE" for h in store.documents("core_health_events"))
        broker.get_account = original
        clocks.advance(1)
        decision = await trader_cycle(
            store, broker, fresh(observation, clocks, sequence=3), model, proof, now=clocks.wall
        )
        assert len(broker.posts) == 1, decision.gates
        assert any(h.get("recovered_from") for h in store.documents("core_health_events"))
        assert all(h in store.documents("core_health_events") for h in failed)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_valid_unowned_quantity_stays_quarantined_after_identical_snapshot(tmp_path):
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        broker.quantities["XLE"] = Decimal(1)
        broker.basis["XLE"] = Decimal(100)
        await trader_cycle(store, broker, observation, model, proof, now=clocks.wall)
        clocks.advance(1)
        await trader_cycle(store, broker, fresh(observation, clocks), model, proof, now=clocks.wall)
        assert not broker.posts
        assert any(h["state"] == "INTEGRITY_FAILURE" for h in store.documents("core_health_events"))
        # Returning to a superficially matching raw observation is not audited resolution.
        broker.quantities["XLE"] = Decimal(0)
        clocks.advance(1)
        await trader_cycle(
            store, broker, fresh(observation, clocks, sequence=3), model, proof, now=clocks.wall
        )
        assert not broker.posts
    finally:
        db.close()


@pytest.mark.asyncio
async def test_backward_wall_jump_recovery_uses_new_generation_not_utc_order(tmp_path):
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        clocks.advance(10)
        clocks.wall += timedelta(seconds=1)
        wait = await trader_cycle(
            store, broker, fresh(observation, clocks), model, proof, now=clocks.wall
        )
        assert wait.action == "WAIT" and not broker.posts
        failed_at = clocks.wall
        clocks.wall -= timedelta(seconds=1)
        resumed = await trader_cycle(
            store, broker, fresh(observation, clocks, sequence=3), model, proof, now=clocks.wall
        )
        assert clocks.wall < failed_at
        assert resumed.action == "BUY" and len(broker.posts) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_429_clock_fault_returns_scoped_wait_and_can_recover(tmp_path):
    from quantbot.brokers.base import BrokerRequestError
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        broker.clock_fault = BrokerRequestError("synthetic429", status_code=429)
        assert (
            await trader_cycle(store, broker, observation, model, proof, now=clocks.wall)
        ).action == "WAIT"
        assert not broker.posts
        broker.clock_fault = None
        clocks.advance(1)
        assert (
            await trader_cycle(
                store, broker, fresh(observation, clocks), model, proof, now=clocks.wall
            )
        ).action == "BUY"
        assert len(broker.posts) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_guardian_provider_outage_records_retry_without_disabling_outcome_poll(tmp_path):
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=clocks.wall)
        clocks.advance(1801)

        async def unavailable():
            raise TimeoutError("synthetic outage")

        broker.get_account = unavailable
        receipt = await guardian_cycle(store, broker, now=broker.now)
        assert receipt.reasons and not receipt.exit_intent_ids
        assert len(broker.posts) == 1
        assert store.documents("core_cycle_receipts")
    finally:
        db.close()
