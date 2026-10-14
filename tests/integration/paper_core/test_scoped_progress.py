"""Joined entry waits, independent owned exits/outcomes and evidence-only recovery."""

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.fixtures.core_m2 import NOW, fee_events
from tests.fixtures.core_m3 import Clocks, fresh, prepare


@pytest.mark.asyncio
async def test_same_observation_replay_cannot_post_again_after_completed_exit(tmp_path):
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.outcomes import import_broker_events
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        first = await trader_cycle(store, broker, observation, model, proof, now=clocks.wall)
        broker.set_mark("XLE", Decimal(98), as_of=broker.now)
        await guardian_cycle(store, broker, now=broker.now)
        import_broker_events(store, fee_events(store, broker))
        again = await trader_cycle(
            store, broker, observation, model, store.latest_proof(), now=clocks.wall
        )
        assert first.id == again.id and len(broker.posts) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_joined_timeline_waits_preserve_exit_outcome_and_fresh_recovery(tmp_path):
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.outcomes import import_broker_events, process_outcomes
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.research.manifest import canonical_result_json
    from quantbot.storage import StorageRepository

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    timeline = []

    def witness(stage, decision=None):
        timeline.append(
            {
                "stage": stage,
                "posts": len(broker.posts),
                "at": broker.now,
                "decision_id": decision.id if decision else None,
                "gates": decision.gates if decision else (),
                "health": store.documents("core_health_events"),
                "outcomes": store.documents("core_outcome_revisions"),
            }
        )

    try:
        entry = await trader_cycle(store, broker, observation, model, proof, now=clocks.wall)
        assert len(broker.posts) == 1
        witness("valid-entry", entry)
        broker.clock_fault = TimeoutError("synthetic timeout")
        clocks.advance(1)
        refusal = await trader_cycle(
            store, broker, fresh(observation, clocks), model, proof, now=clocks.wall
        )
        assert refusal.action == "WAIT" and len(broker.posts) == 1
        witness("provider-timeout", refusal)
        clocks.advance(1800)
        broker.now = NOW + timedelta(seconds=clocks.mono - 100)
        receipt = await guardian_cycle(
            store, broker, now=NOW + timedelta(seconds=clocks.mono - 100)
        )
        assert receipt.exit_intent_ids and len(broker.posts) == 2
        broker.now = NOW + timedelta(seconds=clocks.mono - 100)
        import_broker_events(store, fee_events(store, broker))
        assert process_outcomes(store, cutoff=broker.now).outcomes[0].status == "COMPLETE"
        witness("independent-exit-and-costed-outcome")
        broker.clock_fault = None
        broker.set_mark("XLU", Decimal(123), as_of=NOW - timedelta(days=1))
        clocks.advance(1)
        resumed = await trader_cycle(
            store, broker, fresh(observation, clocks, sequence=3), model, proof, now=clocks.wall
        )
        assert len(broker.posts) == 3 and resumed.action == "BUY"
        witness("unrelated-stale-symbol-fresh-recovery", resumed)
        clocks.advance(6 * 3600)
        closed = await trader_cycle(
            store, broker, fresh(observation, clocks, sequence=4), model, proof, now=clocks.wall
        )
        assert closed.action == "WAIT" and len(broker.posts) == 3
        witness("market-close", closed)
        clocks.wall -= timedelta(hours=1)
        jumped = await trader_cycle(
            store, broker, fresh(observation, clocks, sequence=5), model, proof, now=clocks.wall
        )
        assert jumped.action == "WAIT" and len(broker.posts) == 3
        witness("wall-jump", jumped)
        # Owned exit and outcome polling continue during the closed-session wait.
        receipt = await guardian_cycle(store, broker, now=broker.now)
        assert receipt.exit_intent_ids and len(broker.posts) == 4
        old_fills = {
            f
            for trip in store.documents("core_outcome_revisions")
            if trip["status"] == "COMPLETE"
            for f in trip["entry_fill_ids"] + trip["exit_fill_ids"]
        }
        import_broker_events(
            store, tuple(e for e in fee_events(store, broker) if e.cost.fill_id not in old_fills)
        )
        assert process_outcomes(store, cutoff=broker.now).outcomes[0].status == "COMPLETE"
        witness("closed-session-owned-exit-and-outcome")
        clocks.wall += timedelta(hours=1)
        clocks.advance((NOW + timedelta(days=1) - clocks.wall).total_seconds())
        broker.clock_session_id = "2026-10-15"
        broker.clock_session_open += timedelta(days=1)
        broker.clock_session_close += timedelta(days=1)
        next_observation = fresh(observation, clocks, sequence=6).model_copy(
            update={"session_close": broker.clock_session_close}
        )
        recovered = await trader_cycle(
            store, broker, next_observation, model, proof, now=clocks.wall
        )
        assert recovered.action == "BUY" and len(broker.posts) == 5
        witness("fresh-next-session-recovery", recovered)
        with db.transaction() as session:
            assert not StorageRepository(session).get_kill_switch_state().engaged
        print(
            "M3_TIMELINE:"
            + canonical_result_json(
                {
                    "timeline": timeline,
                    "entry_loop": store.export_loop(entry.id),
                    "posts": broker.posts,
                    "time_evidence": store.documents("core_time_evidence"),
                    "unauthorized_posts": 0,
                    "lane": "SYNTHETIC_TEST",
                }
            )
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_missing_spy_dependency_blocks_only_its_decision(tmp_path):
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        missing = observation.model_copy(update={"dependency_available_at": (None,)})
        decision = await trader_cycle(store, broker, missing, model, proof, now=clocks.wall)
        assert "DEPENDENCY_UNAVAILABLE" in decision.gates and not broker.posts
        clocks.advance(1)
        await trader_cycle(store, broker, fresh(observation, clocks), model, proof, now=clocks.wall)
        assert len(broker.posts) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unrelated_unowned_symbol_does_not_disable_known_owned_exit(tmp_path):
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=clocks.wall)
        broker.quantities["XLU"] = Decimal(1)
        broker.basis["XLU"] = Decimal(100)
        clocks.advance(1801)
        receipt = await guardian_cycle(store, broker, now=broker.now)
        assert receipt.exit_intent_ids and broker.posts[-1].symbol == "XLE"
        assert broker.posts[-1].quantity == 5 and broker.quantities["XLU"] == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_manual_pause_remains_first_blocker_alongside_market_close(tmp_path):
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.storage import StorageRepository

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        with db.transaction() as session:
            StorageRepository(session).set_kill_switch(
                True, reason="explicit synthetic operator pause", updated_at=NOW
            )
        clocks.advance(7 * 3600)
        refused = await trader_cycle(
            store, broker, fresh(observation, clocks), model, proof, now=clocks.wall
        )
        assert refused.gates[0] == "CORE_MANUAL_ENTRY_PAUSE"
        assert "TIME_SESSION_BOUNDARY" in refused.gates and not broker.posts
        with db.transaction() as session:
            assert (
                StorageRepository(session).get_kill_switch_state().reason
                == "explicit synthetic operator pause"
            )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_restart_guardian_uses_fresh_own_process_clock_and_durable_deadline(tmp_path):
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.store import CoreStore
    from quantbot.paper_core.time_evidence import BrokerTime
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=clocks.wall)
        new = Clocks()
        new.process_id = "independent-guardian-process"
        new.mono = 9999999
        new.wall = NOW + timedelta(seconds=1801)

        async def sample():
            new.advance(0.01)
            broker.now = new.wall
            return BrokerTime(
                timestamp=broker.now,
                account_id=broker.account_id,
                provider=broker.provider,
                session_id="2026-10-14",
                session_open=NOW - timedelta(hours=1),
                session_close=observation.session_close,
                resolution_seconds=0.001,
            )

        broker.get_clock = sample
        peer = CoreStore(db, store.config, clocks=new)
        assert (await guardian_cycle(peer, broker, now=new.wall)).exit_intent_ids
        assert len(broker.posts) == 2 and broker.posts[-1].quantity == 5
        print(
            "M3_RESTART:"
            + json.dumps(
                {
                    "trader_process": clocks.process_id,
                    "guardian_process": new.process_id,
                    "guardian_monotonic": new.mono,
                    "posts": len(broker.posts),
                }
            )
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_closed_lot_conflict_blocks_dependent_cash_but_independent_cash_allows_entry(
    tmp_path,
):
    from quantbot.paper_core.contracts import BrokerEvent, CashActivity
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.outcomes import import_broker_events, process_outcomes
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.research.manifest import content_id
    from quantbot.storage import StorageRepository

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        first = await trader_cycle(store, broker, observation, model, proof, now=clocks.wall)
        clocks.advance(1801)
        await guardian_cycle(store, broker, now=broker.now)
        import_broker_events(store, fee_events(store, broker))
        assert process_outcomes(store, cutoff=broker.now).outcomes[0].status == "COMPLETE"
        with db.transaction() as session:
            StorageRepository(session).create_incident(
                "synthetic-disputed-cost",
                severity="ERROR",
                kind="CORE_EVIDENCE_CONFLICT",
                message="disputed closed-lot fee source",
                occurred_at=broker.now,
                detail={
                    "lot_ids": [first.id],
                    "implicated_source_ids": [fee_events(store, broker)[0].id],
                    "disputed_authorities": ["CASH"],
                },
            )
        refused = await trader_cycle(
            store, broker, fresh(observation, clocks), model, proof, now=clocks.wall
        )
        assert refused.action == "WAIT" and len(broker.posts) == 2
        activity = CashActivity(
            recorded_at_utc=broker.now,
            account_id=store.config.account_id,
            document_id="synthetic-independent-cash",
            activity_ids=("buy-cash", "sell-cash"),
            complete=True,
            watermark=broker.now,
            movements=tuple(
                (f.fill_id, (-1 if f.side.value == "BUY" else 1) * f.quantity * f.price)
                for f in broker.fills
            ),
        )
        import_broker_events(
            store,
            (
                BrokerEvent(
                    recorded_at_utc=broker.now,
                    occurred_at=broker.now,
                    account_id=store.config.account_id,
                    endpoint="synthetic/cash",
                    event_id=activity.id,
                    raw_digest=content_id(activity),
                    client_order_id=broker.posts[0].client_order_id,
                    cash_activity=activity,
                ),
            ),
        )
        clocks.advance(1)
        resumed = await trader_cycle(
            store, broker, fresh(observation, clocks, sequence=3), model, proof, now=clocks.wall
        )
        assert resumed.action == "BUY" and len(broker.posts) == 3
    finally:
        db.close()


@pytest.mark.asyncio
async def test_populated_accepted_v31_migration_preserves_payload_ids_and_claim_replay(tmp_path):
    from quantbot.paper_core.contracts import (
        CoreConfig,
        Decision,
        Observation,
        ProtectionPlan,
        RoundTrip,
    )
    from quantbot.paper_core.store import CoreStore, table
    from quantbot.storage import Database

    fixture = json.loads((Path(__file__).parents[2] / "fixtures/core_v31_m2.json").read_text())
    from sqlalchemy import create_engine

    engine = create_engine("sqlite:///" + str(tmp_path / "v31.db"))
    from alembic import command

    with engine.begin() as connection:
        command.upgrade(Database._alembic_config(connection), "0031")
        for name, rows in fixture.items():
            if name in {"kill_switch_state", "schema_version"}:
                connection.exec_driver_sql(f"DELETE FROM {name}")
            for row in rows:
                keys = tuple(row)
                connection.exec_driver_sql(
                    f"INSERT INTO {name} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                    tuple(row[k] for k in keys),
                )
    engine.dispose()
    with Database.migrate(tmp_path / "v31.db"):
        pass
    with Database(tmp_path / "v31.db") as db:
        store = CoreStore(
            db, CoreConfig.model_validate_json(fixture["core_identities"][0]["payload"])
        )
        for name, kind in (
            ("core_observations", Observation),
            ("core_decisions", Decision),
            ("core_protection_plans", ProtectionPlan),
            ("core_outcome_revisions", RoundTrip),
        ):
            for row in fixture[name]:
                original = kind.model_validate_json(row["payload"])
                assert original.id == row["id"]
                assert store.read(name, row["id"], kind).id == row["id"]
                with db.transaction() as session:
                    assert (
                        session.execute(
                            select(table(name).c.payload).where(table(name).c.id == row["id"])
                        ).scalar_one()
                        == row["payload"]
                    )
        assert store.links() == fixture["core_intent_links"]
        assert store.documents("core_health_events") == []
        from quantbot.paper_core.contracts import ModelArtifact
        from quantbot.paper_core.simulation import SyntheticBroker
        from quantbot.paper_core.trader import trader_cycle

        original = Decision.model_validate_json(fixture["core_decisions"][0]["payload"])
        observation = store.read("core_observations", original.observation_id, Observation)
        broker = SyntheticBroker(
            account_id=store.config.account_id, now=NOW + timedelta(minutes=30)
        )
        assert (
            await trader_cycle(
                store, broker, observation, ModelArtifact(), store.latest_proof(), now=broker.now
            )
            == original
        )
        assert not broker.posts
