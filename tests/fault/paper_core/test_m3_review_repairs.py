"""Independent M3 review regressions on the production trader/guardian path."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from tests.fixtures.core_m2 import NOW
from tests.fixtures.core_m3 import fresh, prepare


@pytest.mark.asyncio
@pytest.mark.parametrize("account_delay,position_delay", [(0.001, 0), (0, 0.001), (0.25, 0.25)])
async def test_positive_inventory_latency_enters_with_reproducible_current_projection(
    tmp_path, account_delay, position_delay
):
    from quantbot.paper_core.contracts import ReconciliationProof
    from quantbot.paper_core.time_evidence import TimeEvidence
    from quantbot.paper_core.trader import evaluate_entry, trader_cycle
    from quantbot.research.manifest import canonical_result_json

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    account, positions = broker.get_account, broker.get_positions

    async def delayed_account():
        clocks.advance(account_delay)
        broker.now = clocks.wall
        return await account()

    async def delayed_positions():
        clocks.advance(position_delay)
        broker.now = clocks.wall
        return await positions()

    broker.get_account, broker.get_positions = delayed_account, delayed_positions
    try:
        decision = await trader_cycle(store, broker, observation, model, proof, now=clocks.wall)
        assert decision.action == "BUY" and len(broker.posts) == 1, decision.gates
        origin = store.read("core_time_evidence", decision.time_evidence_id, TimeEvidence)
        consumed = store.read(
            "core_reconciliation_proofs", decision.inventory_id, ReconciliationProof
        )
        assert origin.wall_anchor < consumed.recorded_at_utc <= decision.recorded_at_utc
        assert (
            evaluate_entry(
                store.config,
                observation,
                model,
                consumed,
                time_evidence=origin,
                time_projection=decision.time_projection,
            )
            == decision
        )
        assert store.read("core_time_evidence", origin.id, TimeEvidence) == origin
        print(
            "M3_FIX1:"
            + canonical_result_json(
                {
                    "case": "positive-inventory-latency",
                    "account_delay": account_delay,
                    "position_delay": position_delay,
                    "decision": decision,
                    "inventory": consumed,
                    "original_time": origin,
                    "posts": broker.posts,
                }
            )
        )
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["expired", "wall-jump", "future-observation", "future-proof"])
async def test_current_projection_never_revives_expired_discontinuous_or_future_evidence(
    tmp_path, fault
):
    from quantbot.paper_core import guardian
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    original = guardian.synchronize

    async def delayed(store, broker, *, now):
        result = await original(store, broker, now=now)
        if fault == "expired":
            clocks.advance(121)
        if fault == "wall-jump":
            clocks.wall += timedelta(seconds=0.051)
        return result

    # A completed acquisition followed by a stalled caller tests proof lifetime,
    # rather than misclassifying a >5s acquisition as a complete proof.
    from unittest.mock import patch

    from quantbot.paper_core import trader

    original_acquire = trader.acquire_time_evidence

    async def acquire(broker, clocks):
        value = await original_acquire(broker, clocks)
        if fault == "future-proof":
            return value.model_copy(
                update={
                    "wall_anchor": clocks.wall + timedelta(seconds=1),
                    "acquired_monotonic": clocks.mono + 1,
                }
            )
        return value

    try:
        if fault == "future-observation":
            observation = observation.model_copy(
                update={
                    "source_end": NOW + timedelta(seconds=1),
                    "available_at": NOW + timedelta(seconds=1),
                }
            )
        with (
            patch.object(guardian, "synchronize", delayed),
            patch.object(trader, "acquire_time_evidence", acquire),
        ):
            refused = await trader_cycle(store, broker, observation, model, proof, now=clocks.wall)
        expected = {
            "expired": "TIME_EVIDENCE_EXPIRED",
            "wall-jump": "TIME_DISCONTINUITY",
            "future-observation": "NONCAUSAL_OBSERVATION",
            "future-proof": "TIME_EVIDENCE_EXPIRED",
        }[fault]
        assert refused.action == "WAIT" and expected in refused.gates and not broker.posts
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("offset,elapsed", [(1.9, 8.2), (-1.9, 11.9)])
async def test_close_clipped_monotonic_exit_during_time_outage_uses_matching_domains(
    tmp_path, offset, elapsed
):
    from quantbot.paper_core.contracts import ProtectionPlan
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.research.manifest import canonical_result_json

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        clocks.advance(6 * 3600 - 120 - 10)
        broker.offset = offset
        observation = fresh(observation, clocks).model_copy(
            update={"source_end": clocks.wall - timedelta(seconds=2), "available_at": clocks.wall}
        )
        decision = await trader_cycle(store, broker, observation, model, proof, now=clocks.wall)
        assert len(broker.posts) == 1, decision.gates
        plan = store.read(
            "core_protection_plans", store.links()[0]["protection_id"], ProtectionPlan
        )
        assert plan.deadline == observation.session_close - timedelta(seconds=120)
        broker.clock_fault = TimeoutError("synthetic reviewed clock outage")
        clocks.advance(elapsed)
        broker.now = clocks.wall + timedelta(seconds=offset)
        receipt = await guardian_cycle(store, broker, now=broker.now)
        assert receipt.exit_intent_ids and len(broker.posts) == 2
        assert broker.posts[-1].quantity == 5
        print(
            "M3_FIX1:"
            + canonical_result_json(
                {
                    "case": "close-monotonic-domain",
                    "offset": offset,
                    "elapsed": elapsed,
                    "plan": plan,
                    "provider_now": broker.now,
                    "receipt": receipt,
                    "posts": broker.posts,
                }
            )
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_retained_closed_wait_cannot_hide_dependent_integrity_incident(tmp_path):
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.research.manifest import canonical_result_json
    from quantbot.storage import StorageRepository

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        clocks.advance(7 * 3600)
        first = await trader_cycle(
            store, broker, fresh(observation, clocks), model, proof, now=clocks.wall
        )
        assert first.action == "WAIT" and "TIME_SESSION_BOUNDARY" in first.gates
        with db.transaction() as session:
            StorageRepository(session).create_incident(
                "review-dependent-account-conflict",
                severity="ERROR",
                kind="CORE_EVIDENCE_CONFLICT",
                message="synthetic unresolved account authority",
                occurred_at=broker.now,
                detail={
                    "account_scope": True,
                    "lot_ids": [],
                    "disputed_authorities": ["CASH", "OWNERSHIP"],
                },
            )
        clocks.advance(1)
        second = await trader_cycle(
            store, broker, fresh(observation, clocks, sequence=3), model, proof, now=clocks.wall
        )
        assert second.gates[0] == "CORE_EVIDENCE_QUARANTINED"
        assert "TIME_SESSION_BOUNDARY" in second.gates and not broker.posts
        print(
            "M3_FIX1:"
            + canonical_result_json(
                {
                    "case": "unified-integrity-priority",
                    "first": first,
                    "second": second,
                    "health": store.documents("core_health_events"),
                }
            )
        )
    finally:
        db.close()


def test_additive_repair_fields_preserve_original_v32_timing_payload_ids():
    from quantbot.paper_core.contracts import CoreConfig, Decision, ProtectionPlan
    from quantbot.research.manifest import content_id

    old = json.loads(
        (Path(__file__).parents[3] / "docs/evidence/paper-core/m3/joined-timeline.json").read_text()
    )["entry_loop"]
    decision = Decision.model_validate(old["decision"])
    assert decision.id == old["decision_id"]
    plan = ProtectionPlan.for_decision(decision, CoreConfig.model_validate(old["config"]))
    assert plan.id == old["links"][0]["protection_id"]
    assert content_id(plan) == plan.id


def test_old_v2_plan_without_expiry_uses_conservative_provider_bound():
    from quantbot.paper_core.contracts import ProtectionPlan
    from quantbot.paper_core.time_evidence import exit_due

    plan = ProtectionPlan(
        recorded_at_utc=NOW,
        owner="synthetic",
        generation=1,
        inventory_generation=1,
        decision_id="old-v32-owned",
        stop_price=99,
        deadline=NOW + timedelta(seconds=8.1),
        clock_profile="PAPER_TIME_V2",
        clock_process_id="original-process",
        monotonic_anchor=100,
    )
    # With no original bracket supplied, +2s is the approved latest possible
    # provider origin. 8.1 provider seconds minus that origin gives 6.1 elapsed.
    assert exit_due(plan, None, 6.1)


@pytest.mark.asyncio
async def test_old_v32_plan_uses_original_provider_bracket_during_time_outage(
    tmp_path, monkeypatch
):
    from quantbot.paper_core import trader
    from quantbot.paper_core.contracts import Decision, ProtectionPlan
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.research.manifest import canonical_result_json

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    original = trader.evaluate_entry

    def old_document(*args, **kwargs):
        data = original(*args, **kwargs).model_dump()
        data.pop("time_projection", None)
        return Decision.model_validate(data)

    monkeypatch.setattr(trader, "evaluate_entry", old_document)
    try:
        clocks.advance(6 * 3600 - 120 - 10)
        broker.offset = 1.9
        decision = await trader.trader_cycle(
            store, broker, fresh(observation, clocks), model, proof, now=clocks.wall
        )
        assert len(broker.posts) == 1
        plan = store.read(
            "core_protection_plans", store.links()[0]["protection_id"], ProtectionPlan
        )
        before = canonical_result_json(plan)
        assert "monotonic_expiry" not in json.loads(before)
        broker.clock_fault = TimeoutError("old-v32 clock outage")
        clocks.advance(8.2)
        broker.now = clocks.wall + timedelta(seconds=1.9)
        assert (await guardian_cycle(store, broker, now=broker.now)).exit_intent_ids
        assert (
            canonical_result_json(store.read("core_protection_plans", plan.id, ProtectionPlan))
            == before
        )
        assert store.read("core_decisions", decision.id, Decision) == decision
        print(
            "M3_FIX1:"
            + canonical_result_json(
                {
                    "case": "old-v32-expiry-compatibility",
                    "plan": plan,
                    "decision": decision,
                    "posts": broker.posts,
                }
            )
        )
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [1.9, -1.9])
async def test_close_expiry_boundary_exits_at_conservative_monotonic_expiry(tmp_path, offset):
    from quantbot.paper_core.contracts import ProtectionPlan
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.research.manifest import canonical_result_json

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        clocks.advance(6 * 3600 - 120 - 10)
        broker.offset = offset
        observed = fresh(observation, clocks).model_copy(
            update={"source_end": clocks.wall - timedelta(seconds=2)}
        )
        decision = await trader_cycle(store, broker, observed, model, proof, now=clocks.wall)
        assert len(broker.posts) == 1, decision.gates
        plan = store.read(
            "core_protection_plans", store.links()[0]["protection_id"], ProtectionPlan
        )
        assert plan.monotonic_expiry is not None
        broker.clock_fault = TimeoutError("exact expiry outage")
        clocks.advance(plan.monotonic_expiry - clocks.mono - 0.001)
        broker.now = clocks.wall + timedelta(seconds=offset)
        assert not (await guardian_cycle(store, broker, now=broker.now)).exit_intent_ids
        clocks.advance(plan.monotonic_expiry - clocks.mono)
        broker.now = clocks.wall + timedelta(seconds=offset)
        assert (await guardian_cycle(store, broker, now=broker.now)).exit_intent_ids
        assert clocks.mono == plan.monotonic_expiry and len(broker.posts) == 2
        print(
            "M3_FIX1:"
            + canonical_result_json(
                {
                    "case": "exact-close-expiry",
                    "offset": offset,
                    "plan": plan,
                    "exit_monotonic": clocks.mono,
                    "provider_now": broker.now,
                    "posts": broker.posts,
                }
            )
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_final_send_cannot_outlive_monotonic_protection_expiry(tmp_path):
    from quantbot.paper_core.send_guard import definitively_unsent
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.storage import StorageRepository

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    original = broker.get_positions
    reads = 0

    async def last_fetch():
        nonlocal reads
        reads += 1
        if reads == 4:
            clocks.advance(8.08)
            # A 40ms movement is inside the unchanged 50ms continuity bound.
            # Provider-wall projection is just before close while holding
            # duration has already reached the conservative monotonic expiry.
            clocks.wall -= timedelta(seconds=0.04)
            broker.now = clocks.wall
        return await original()

    broker.get_positions = last_fetch
    try:
        clocks.advance(6 * 3600 - 120 - 10)
        broker.offset = 1.9
        await trader_cycle(store, broker, fresh(observation, clocks), model, proof, now=clocks.wall)
        assert not broker.posts
        with db.transaction() as session:
            assert definitively_unsent(StorageRepository(session), store.links()[0]["intent_id"])
    finally:
        db.close()
