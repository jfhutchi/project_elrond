"""Fixture-only joined mechanics; no trading or economic-performance claim."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quantbot.storage import Database, StorageRepository

NOW = datetime(2026, 10, 14, 14, 0, tzinfo=UTC)


def prepare(tmp_path, *, inherited=False):
    from quantbot.paper_core.contracts import CoreConfig, ModelArtifact, Observation
    from quantbot.paper_core.runtime import initialize_fixture
    from quantbot.paper_core.simulation import SyntheticBroker
    from quantbot.paper_core.store import CoreStore

    config = CoreConfig(account_id="synthetic-account", symbol="XLE")
    db = Database.create(tmp_path / "core.db")
    broker = SyntheticBroker(
        account_id=config.account_id, now=NOW, inherited={"SPY": Decimal("2")} if inherited else {}
    )
    store = CoreStore(db, config)
    proof = initialize_fixture(store, broker, now=NOW, entry_enabled=True)
    observation = Observation(
        symbol="XLE",
        source_start=NOW - timedelta(minutes=5),
        source_end=NOW,
        available_at=NOW,
        recorded_at_utc=NOW,
        price=Decimal("100"),
        raw_data_digest="synthetic-bar-001",
        features=(("return", 0.01),),
        dependencies=("SPY",),
        dependency_available_at=(NOW,),
        sequence=1,
        session_close=NOW + timedelta(hours=6),
    )
    return db, store, broker, proof, observation, ModelArtifact()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["deadline", "stop"])
async def test_observation_to_guardian_exit_is_owned_terminal_flat_and_costs_pending(
    tmp_path, trigger
):
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.store import CoreStore
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model = prepare(tmp_path, inherited=True)
    try:
        decision = await trader_cycle(store, broker, observation, model, proof, now=NOW)
        assert decision.action == "BUY"
        assert len(broker.posts) == 1
        assert broker.posts[0].quantity == Decimal("5")  # $100k * 0.5% / $100
        with db.transaction() as session:
            assert len(StorageRepository(session).list_fills()) == 1
        # Lose trader object and reopen store: guardian cannot depend on its memory.
        config, path = store.config, db.path
        db.close()
        db = Database(path)
        store = CoreStore(db, config)
        broker.now = (
            NOW + timedelta(minutes=30) if trigger == "deadline" else NOW + timedelta(seconds=15)
        )
        broker.set_mark("XLE", Decimal("98"), as_of=broker.now)
        receipt = await guardian_cycle(store, broker, now=broker.now)
        assert receipt.exit_intent_ids
        assert len(broker.posts) == 2
        assert broker.posts[1].quantity == Decimal("5")
        assert await broker.get_position("XLE") is None
        assert (await broker.get_position("SPY")).quantity == Decimal("2")
        bundle = store.export_loop(decision.id)
        assert bundle["round_trip"]["status"] == "COSTS_PENDING"
        assert bundle["round_trip"]["net_pnl"] is None
        assert bundle["round_trip"]["gross_pnl"] == "-10"
        assert bundle["round_trip"]["open_quantity"] == "0"
        assert len(bundle["fills"]) == 2
        assert all(order["status"] == "filled" for order in bundle["orders"])
        await guardian_cycle(store, broker, now=broker.now + timedelta(seconds=15))
        assert len(broker.posts) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_duplicate_observation_has_one_entry_post_and_immutable_decision(tmp_path):
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        first = await trader_cycle(store, broker, observation, model, proof, now=NOW)
        second = await trader_cycle(store, broker, observation, model, proof, now=NOW)
        assert first == second
        assert len(broker.posts) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_manual_pause_after_decision_before_claim_prevents_post(tmp_path):
    from quantbot.paper_core.contracts import ProtectionPlan
    from quantbot.paper_core.trader import claim_entry, evaluate_entry, record_observation

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        record_observation(store, observation)
        decision = evaluate_entry(store.config, observation, model, proof)
        with db.transaction() as session:
            StorageRepository(session).set_kill_switch(
                True, reason="operator pause", updated_at=NOW
            )
        assert (
            claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
            is None
        )
        assert broker.posts == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_pause_arriving_during_final_account_fetch_prevents_post(tmp_path):
    from quantbot.execution import OrderSubmissionBlocked
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model = prepare(tmp_path)
    original = broker.get_account
    count = 0

    async def pause_on_final_fetch():
        nonlocal count
        count += 1
        if count == 2:
            with db.transaction() as session:
                StorageRepository(session).set_kill_switch(
                    True, reason="operator pause", updated_at=NOW
                )
        return await original()

    broker.get_account = pause_on_final_fetch
    try:
        with pytest.raises(OrderSubmissionBlocked):
            await trader_cycle(store, broker, observation, model, proof, now=NOW)
        assert broker.posts == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unowned_quantity_never_certifies_by_repetition_or_exits(tmp_path):
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.quantities["XLE"] += Decimal("1")
        broker.now = NOW + timedelta(minutes=30)
        for offset in (0, 15):
            receipt = await guardian_cycle(
                store, broker, now=broker.now + timedelta(seconds=offset)
            )
            assert "INVENTORY_MISMATCH" in receipt.reasons
            assert not receipt.exit_intent_ids
        assert len(broker.posts) == 1
        assert store.latest_proof().actual_quantities == (("XLE", Decimal("5")),)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_durable_decision_protection_and_guardian_ack_exist_before_post(tmp_path):
    from sqlalchemy import select

    from quantbot.paper_core.store import table
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model = prepare(tmp_path)
    original = broker.submit_order

    async def inspect_before_post(intent):
        with db.transaction() as session:
            repo = StorageRepository(session)
            assert repo.get_order_intent(intent.intent_id).state.value == "SUBMITTING"
            link = (
                session.execute(
                    select(table("core_intent_links")).where(
                        table("core_intent_links").c.intent_id == intent.intent_id
                    )
                )
                .mappings()
                .one()
            )
            assert store.documents("core_decisions")
            assert store.read(
                "core_protection_plans",
                link["protection_id"],
                __import__(
                    "quantbot.paper_core.contracts", fromlist=["ProtectionPlan"]
                ).ProtectionPlan,
            )
            assert any(
                link["protection_id"] in r["protection_ids"]
                for r in store.documents("core_guardian_receipts")
            )
        return await original(intent)

    broker.submit_order = inspect_before_post
    try:
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        assert len(broker.posts) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_forged_oversized_decision_cannot_claim(tmp_path):
    from quantbot.paper_core.contracts import ProtectionPlan
    from quantbot.paper_core.trader import claim_entry, evaluate_entry, record_observation

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        record_observation(store, observation)
        decision = evaluate_entry(store.config, observation, model, proof)
        forged = decision.model_copy(update={"quantity": Decimal("1000")})
        with pytest.raises(ValueError, match="decision"):
            claim_entry(store, forged, ProtectionPlan.for_decision(forged, store.config))
        with db.transaction() as session:
            assert not StorageRepository(session).list_unresolved_order_intents()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_stale_stop_mark_waits_but_independent_deadline_still_exits(tmp_path):
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.set_mark("XLE", Decimal("98"), as_of=NOW)
        broker.now = NOW + timedelta(minutes=5)
        assert not (await guardian_cycle(store, broker, now=broker.now)).exit_intent_ids
        broker.now = NOW + timedelta(minutes=30)
        assert (await guardian_cycle(store, broker, now=broker.now)).exit_intent_ids
        assert len(broker.posts) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_full_round_trip_releases_single_position_slot(tmp_path):
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.now = NOW + timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        later = observation.model_copy(
            update={
                "recorded_at_utc": broker.now,
                "available_at": broker.now,
                "source_start": broker.now - timedelta(minutes=5),
                "source_end": broker.now,
                "sequence": 2,
                "raw_data_digest": "synthetic-bar-002",
                "dependency_available_at": (broker.now,),
            }
        )
        await trader_cycle(store, broker, later, model, store.latest_proof(), now=broker.now)
        assert len(broker.posts) == 3
        assert broker.posts[2].quantity == Decimal("5")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_certification_keeps_non_100_fill_basis_and_owned_stop(tmp_path):
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        observation = observation.model_copy(update={"price": Decimal("125")})
        broker.set_mark("XLE", Decimal("125"), as_of=NOW)
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        with db.transaction() as session:
            baseline = StorageRepository(session).get_latest_account_snapshot(
                store.config.account_id, certified_only=True
            )
            assert baseline.positions[0].average_entry_price == Decimal("125")
        broker.now = NOW + timedelta(seconds=15)
        broker.set_mark("XLE", Decimal("123"), as_of=broker.now)
        assert (await guardian_cycle(store, broker, now=broker.now)).exit_intent_ids
        assert broker.posts[1].quantity == Decimal("4")
    finally:
        db.close()


@pytest.mark.parametrize("existing", ["manual_pause", "snapshot"])
def test_fixture_initialization_cannot_clear_historical_stop_or_adopt_snapshot(tmp_path, existing):
    from quantbot.paper_core.contracts import CoreConfig
    from quantbot.paper_core.runtime import initialize_fixture
    from quantbot.paper_core.simulation import SyntheticBroker
    from quantbot.paper_core.store import CoreStore

    with Database.create(tmp_path / "not-empty.db") as db:
        config = CoreConfig(account_id="synthetic-account", symbol="XLE")
        broker = SyntheticBroker(account_id=config.account_id, now=NOW)
        with db.transaction() as session:
            repo = StorageRepository(session)
            if existing == "manual_pause":
                repo.set_kill_switch(True, reason="operator pause", updated_at=NOW)
            else:
                repo.save_account_snapshot(
                    "historical", broker.snapshot().account, (), captured_at=NOW
                )
        store = CoreStore(db, config)
        with pytest.raises(ValueError, match="fixture"):
            initialize_fixture(store, broker, now=NOW)
        with db.transaction() as session:
            assert StorageRepository(session).get_kill_switch_state().engaged


@pytest.mark.asyncio
async def test_separate_guardian_process_exits_while_trader_activity_stalls(tmp_path):
    import asyncio
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    from quantbot.paper_core.trader import trader_cycle
    from quantbot.research.manifest import canonical_result_json

    db, store, broker, proof, observation, model = prepare(tmp_path)
    stalled = None
    try:
        decision = await trader_cycle(store, broker, observation, model, proof, now=NOW)
        state = {
            "now": NOW + timedelta(minutes=30),
            "account_id": broker.account_id,
            "initial_account": broker.initial_account,
            "initial_positions": (),
            "cash": broker.cash,
            "quantities": broker.quantities,
            "basis": broker.basis,
            "orders": broker.orders,
            "fills": broker.fills,
            "config": store.config,
            "decision_id": decision.id,
        }
        state_path = tmp_path / "synthetic-provider.json"
        state_path.write_text(canonical_result_json(state), encoding="utf-8")
        stalled = asyncio.create_task(asyncio.Event().wait())
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[3] / "src")
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("guardian_process.py")),
                str(state_path),
                str(db.path),
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        output = json.loads(result.stdout)
        assert len(output["new_posts"]) == 1
        assert output["new_posts"][0]["side"] == "SELL"
        assert output["new_posts"][0]["quantity"] == "5"
        assert output["bundle"]["round_trip"]["status"] == "COSTS_PENDING"
        assert not stalled.done()
    finally:
        if stalled:
            stalled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stalled
        db.close()


@pytest.mark.asyncio
async def test_same_observation_cannot_reenter_after_immediate_completed_stop(tmp_path):
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.set_mark("XLE", Decimal("98"), as_of=NOW)
        await guardian_cycle(store, broker, now=NOW)
        await trader_cycle(store, broker, observation, model, store.latest_proof(), now=NOW)
        assert len(broker.posts) == 2, "same observation entered again under a newer proof"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_export_joined_synthetic_evidence_from_fixture_store(tmp_path):
    import json

    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.research.manifest import canonical_result_json

    db, store, broker, proof, observation, model = prepare(tmp_path, inherited=True)
    try:
        decision = await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.now = NOW + timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        evidence = {
            "model": model,
            "bundle": store.export_loop(decision.id),
            "provider_posts": broker.posts,
            "final_provider_account": broker.snapshot().account,
            "final_provider_positions": broker.position_snapshot(),
        }
        encoded = canonical_result_json(evidence)
        assert json.loads(encoded)["bundle"]["round_trip"]["net_pnl"] is None
        print("M1_JSON:" + encoded)
    finally:
        db.close()


@pytest.mark.parametrize("field", ["baseline_id", "order_ids", "actual_quantities"])
def test_forged_proof_provenance_or_duplicate_quantity_cannot_certify(tmp_path, field):
    from quantbot.paper_core.store import certify_inventory
    from quantbot.storage import StateConflictError

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        fresh = store.proof(
            broker.snapshot().account, (), positions=(), now=NOW, acquisition_id="synthetic-read"
        )
        replacement = {
            "baseline_id": "unrelated-baseline",
            "order_ids": ("unknown-order",),
            "actual_quantities": (("XLE", Decimal("1")), ("XLE", Decimal("0"))),
        }[field]
        with pytest.raises(StateConflictError):
            certify_inventory(store, fresh.model_copy(update={field: replacement}))
        assert store.latest_proof() == proof
    finally:
        db.close()
