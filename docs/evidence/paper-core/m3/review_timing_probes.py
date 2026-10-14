import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
import pytest
from tests.fixtures.core_m3 import prepare
from quantbot.paper_core.trader import trader_cycle

@pytest.mark.asyncio
async def test_positive_inventory_latency_keeps_healthy_entry_eligible(tmp_path):
    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    original = broker.get_account
    async def delayed():
        clocks.advance(0.001)
        broker.now = clocks.wall
        return await original()
    broker.get_account = delayed
    try:
        decision = await trader_cycle(store, broker, observation, model, proof, now=clocks.wall)
        print('decision', decision.action, decision.gates, decision.recorded_at_utc)
        print('proof', store.latest_proof().recorded_at_utc, 'posts', len(broker.posts))
        assert decision.action == 'BUY' and len(broker.posts) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_positive_offset_close_deadline_during_clock_outage(tmp_path):
    from datetime import timedelta
    from tests.fixtures.core_m3 import fresh
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.contracts import ProtectionPlan
    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        clocks.advance(6*3600 - 120 - 10)
        broker.offset = 1.9
        decision = await trader_cycle(store, broker, fresh(observation, clocks), model, proof, now=clocks.wall)
        assert len(broker.posts) == 1, decision.gates
        plan = store.read('core_protection_plans', store.links()[0]['protection_id'], ProtectionPlan)
        broker.clock_fault = TimeoutError('synthetic clock outage')
        clocks.advance(8.2)
        broker.now = clocks.wall + timedelta(seconds=1.9)
        receipt = await guardian_cycle(store, broker, now=broker.now)
        print('deadline', plan.deadline, 'provider_now', broker.now, 'elapsed', clocks.mono-plan.monotonic_anchor, 'posts', len(broker.posts), 'reasons', receipt.reasons)
        assert len(broker.posts) == 2
    finally:
        db.close()
