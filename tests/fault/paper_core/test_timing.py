"""Relative timing boundaries and durable final-send/owned-exit consumers."""

from datetime import timedelta

import pytest
from tests.fixtures.core_m2 import NOW
from tests.fixtures.core_m3 import fresh, prepare


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "offset,allowed", [(-2.0, True), (2.0, True), (-2.001, False), (2.001, False)]
)
async def test_skew_boundary_controls_actual_entry(tmp_path, offset, allowed):
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        broker.offset = offset

        # Instantaneous transport for exact +/-2.000 boundary, still advancing samples.
        async def sample():
            from quantbot.paper_core.time_evidence import BrokerTime

            broker.now = clocks.wall
            return BrokerTime(
                timestamp=clocks.wall + timedelta(seconds=offset),
                account_id=broker.account_id,
                provider=broker.provider,
                session_id="2026-10-14",
                session_open=observation.recorded_at_utc - timedelta(hours=1),
                session_close=observation.session_close,
                resolution_seconds=0.001,
            )

        broker.get_clock = sample
        original = clocks.sample
        calls = 0

        def advancing():
            nonlocal calls
            calls += 1
            if calls == 4:
                clocks.advance(0.01)
            return original()

        clocks.sample = advancing
        observation = observation.model_copy(
            update={"source_end": observation.source_end - timedelta(seconds=5)}
        )
        decision = await trader_cycle(store, broker, observation, model, proof, now=clocks.wall)
        assert bool(broker.posts) is allowed, decision.gates
        if allowed:
            from quantbot.paper_core.contracts import ProtectionPlan

            plan = store.read(
                "core_protection_plans", store.links()[0]["protection_id"], ProtectionPlan
            )
            assert plan.deadline <= decision.recorded_at_utc + timedelta(seconds=1800 + offset)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_proof_expires_at_120_seconds_restart_and_discontinuity(tmp_path):
    from quantbot.paper_core.time_evidence import acquire_time_evidence, evidence_objections

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        evidence = await acquire_time_evidence(broker, clocks)
        clocks.advance(120)
        assert not evidence_objections(evidence, clocks)
        clocks.advance(0.001)
        assert "TIME_EVIDENCE_EXPIRED" in evidence_objections(evidence, clocks)
        clocks.process_id = "new-process"
        assert "TIME_PROCESS_CHANGED" in evidence_objections(evidence, clocks)
        clocks.process_id = evidence.process_id
        clocks.wall += timedelta(seconds=1)
        assert "TIME_DISCONTINUITY" in evidence_objections(evidence, clocks)
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["frozen", "identity", "disagree", "budget", "jump"])
async def test_two_samples_require_advancement_identity_consistency_budget_and_continuity(
    tmp_path, fault
):
    from quantbot.paper_core.time_evidence import acquire_time_evidence

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    original = broker.get_clock
    count = 0
    first = None

    async def sample():
        nonlocal count, first
        value = await original()
        count += 1
        if count == 1:
            first = value
        else:
            if fault == "frozen":
                value = value.model_copy(update={"timestamp": first.timestamp})
            if fault == "identity":
                value = value.model_copy(update={"session_id": "other-session"})
            if fault == "disagree":
                value = value.model_copy(
                    update={"timestamp": value.timestamp + timedelta(seconds=1)}
                )
            if fault == "budget":
                clocks.advance(5)
            if fault == "jump":
                clocks.wall += timedelta(seconds=0.051)
        return value

    broker.get_clock = sample
    try:
        evidence = await acquire_time_evidence(broker, clocks)
        assert evidence.objections
    finally:
        db.close()


@pytest.mark.asyncio
async def test_delayed_final_fetch_invalidates_entry_and_releases_only_proven_unsent(tmp_path):
    from quantbot.paper_core.send_guard import definitively_unsent
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.storage import StorageRepository

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    original = broker.get_positions
    count = 0

    async def delay():
        nonlocal count
        count += 1
        if count == 4:
            clocks.advance(121)
            broker.now = clocks.wall
        return await original()

    broker.get_positions = delay
    try:
        await trader_cycle(store, broker, observation, model, proof, now=clocks.wall)
        assert broker.posts == []
        with db.transaction() as session:
            assert definitively_unsent(StorageRepository(session), store.links()[0]["intent_id"])
        broker.get_positions = original
        await trader_cycle(
            store, broker, fresh(observation, clocks), model, store.latest_proof(), now=clocks.wall
        )
        assert len(broker.posts) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_wall_jump_and_stale_mark_do_not_extend_monotonic_owned_deadline(tmp_path):
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=clocks.wall)
        broker.set_mark("XLE", 98, as_of=clocks.wall - timedelta(minutes=10))
        clocks.advance(1799)
        clocks.wall -= timedelta(hours=1)
        assert not (await guardian_cycle(store, broker, now=broker.now)).exit_intent_ids
        clocks.advance(1)
        assert (await guardian_cycle(store, broker, now=broker.now)).exit_intent_ids
        assert broker.posts[-1].quantity == 5
    finally:
        db.close()


@pytest.mark.asyncio
async def test_uncertain_interval_overlapping_close_waits_for_entry_and_is_due_for_exit(tmp_path):
    from quantbot.paper_core.contracts import ProtectionPlan
    from quantbot.paper_core.time_evidence import (
        acquire_time_evidence,
        entry_time_objections,
        exit_due,
    )

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        clocks.advance(6 * 3600 - 120 - 0.015)
        evidence = await acquire_time_evidence(broker, clocks)
        observed = fresh(observation, clocks)
        assert "TIME_SESSION_BOUNDARY" in entry_time_objections(observed, evidence, store.config)
        plan = ProtectionPlan(
            recorded_at_utc=NOW,
            decision_id="owned-fixture",
            owner=store.config.owner,
            generation=1,
            inventory_generation=1,
            stop_price=99,
            deadline=NOW + timedelta(hours=6, seconds=-120),
        )
        assert exit_due(plan, evidence, None)
    finally:
        db.close()


def test_positive_provider_skew_never_extends_monotonic_horizon():
    from quantbot.paper_core.contracts import ProtectionPlan
    from quantbot.paper_core.time_evidence import exit_due

    plan = ProtectionPlan(
        recorded_at_utc=NOW,
        decision_id="owned-fixture",
        owner="synthetic",
        generation=1,
        inventory_generation=1,
        stop_price=99,
        deadline=NOW + timedelta(seconds=1802),
        clock_profile="PAPER_TIME_V2",
    )
    assert exit_due(plan, None, 1800)


@pytest.mark.asyncio
async def test_stop_freshness_uses_entire_provider_interval_not_receipt_wall(tmp_path):
    from decimal import Decimal

    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=clocks.wall)
        broker.offset = 1.9
        broker.set_mark("XLE", Decimal(98), as_of=clocks.wall - timedelta(seconds=119))
        assert not (await guardian_cycle(store, broker, now=clocks.wall)).exit_intent_ids
        broker.set_mark("XLE", Decimal(98), as_of=clocks.wall + timedelta(seconds=1.8))
        assert (await guardian_cycle(store, broker, now=clocks.wall)).exit_intent_ids
    finally:
        db.close()
