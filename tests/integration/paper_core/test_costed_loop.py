"""Fee-source truth, immutable economic revisions and additive migration."""

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest
from tests.fixtures.core_m2 import NOW, events, fee_events, prepare


def test_provider_missing_fee_is_unknown_and_zero_requires_authority():
    from quantbot.brokers.alpaca import AlpacaPaperBrokerAdapter

    fill = AlpacaPaperBrokerAdapter._fill_from_payload(
        {
            "activity_type": "FILL",
            "id": "f",
            "order_id": "o",
            "symbol": "XLE",
            "side": "buy",
            "qty": "1",
            "price": "100",
            "transaction_time": NOW.isoformat(),
        }
    )
    assert fill.fee is None, "missing provider fee cannot be a verified zero"
    assert fill.fee_source == "UNKNOWN"


def test_accounting_unknown_zero_never_becomes_cash_or_net():
    from quantbot.brokers import TradeUpdate, TradeUpdateEvent
    from quantbot.domain import BrokerOrder, Fill, OrderSide, OrderType, TimeInForce
    from quantbot.execution.accounting import PortfolioAccountingState, apply_trade_update
    from quantbot.reporting.performance import ReportingDataError, analyze_fills

    fill = Fill(
        fill_id="f",
        broker_order_id="o",
        symbol="XLE",
        side=OrderSide.BUY,
        quantity=1,
        price=100,
        occurred_at=NOW,
        fee=0,
    )
    order = BrokerOrder(
        broker_order_id="o",
        client_order_id="c",
        symbol="XLE",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=1,
        filled_quantity=1,
        status="filled",
        submitted_at=NOW,
    )
    update = TradeUpdate(
        event=TradeUpdateEvent.FILL,
        order=order,
        fill=fill,
        occurred_at=NOW,
        event_id="test-update",
        connection_generation=1,
        position_quantity=Decimal(1),
    )
    state = apply_trade_update(PortfolioAccountingState(cash=1000, realized_pnl=0), update)
    assert state.cash is None and state.realized_pnl is None and state.fees_paid is None
    assert state.positions[0].quantity == 1
    with pytest.raises(ReportingDataError):
        analyze_fills((fill,))


@pytest.mark.asyncio
async def test_late_fees_replace_current_outcome_and_preserve_revisions(tmp_path):
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.outcomes import (
        import_broker_events,
        process_outcomes,
        reconcile_round_trip,
    )
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        decision = await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.now = NOW + timedelta(minutes=30)
        broker.set_mark("XLE", Decimal(102), as_of=broker.now)
        await guardian_cycle(store, broker, now=broker.now)
        import_broker_events(store, events(store, broker))
        assert len(store.documents("core_broker_event_sources")) == 4
        assert not import_broker_events(store, events(store, broker))
        trip = reconcile_round_trip(store, decision.id, as_of=broker.now)
        assert trip.lot_id == decision.id and trip.gross_pnl == Decimal(10)
        assert trip.net_pnl is None and trip.status == "COSTS_PENDING"
        first_fees = fee_events(store, broker)
        import_broker_events(store, first_fees)
        complete = process_outcomes(store, cutoff=broker.now).outcomes[0]
        assert complete.status == "COMPLETE" and complete.net_pnl == Decimal(10)
        assert complete.modeled_net_pnl is None
        assert complete.id != trip.id and complete.supersedes == trip.id
        old_costs = {e.cost.fill_id: e.cost.id for e in first_fees}
        broker.now += timedelta(seconds=1)
        revised = fee_events(store, broker, amount=".10", revision=2, supersedes=old_costs)
        import_broker_events(store, revised)
        latest = process_outcomes(store, cutoff=broker.now).outcomes[0]
        assert latest.net_pnl == Decimal("9.8") and latest.gross_pnl == Decimal(10)
        assert latest.supersedes == complete.id
        assert not process_outcomes(store, cutoff=broker.now).outcomes
        assert len(store.documents("core_outcome_revisions")) == 3
        assert len(store.documents("core_round_trips")) == 1
        assert len(broker.posts) == 2
        print("M2_JSON:" + json.dumps(store.export_loop(decision.id), sort_keys=True))
    finally:
        db.close()


@pytest.mark.asyncio
async def test_event_batch_is_atomic_and_conflicting_replay_refused(tmp_path):
    from quantbot.paper_core.outcomes import import_broker_events
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.storage import StateConflictError

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        batch = events(store, broker)
        bad = batch[-1].model_copy(update={"raw_digest": "conflict"})
        with pytest.raises(StateConflictError):
            import_broker_events(store, (*batch, bad))
        assert len(store.documents("core_broker_event_sources")) == 2
        import_broker_events(store, batch)
        with pytest.raises(StateConflictError):
            import_broker_events(store, (bad,))
        with db.transaction() as session:
            from quantbot.storage import StorageRepository

            incidents = StorageRepository(session).list_incidents(unresolved_only=True)
            assert any(i.kind == "CORE_EVIDENCE_CONFLICT" for i in incidents)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_modeled_slippage_never_deducted_from_actual_net_and_restore_replays(tmp_path):
    from quantbot.paper_core.contracts import CostItem
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.outcomes import import_broker_events, process_outcomes
    from quantbot.paper_core.store import CoreStore
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.research.manifest import content_id
    from quantbot.storage import Database

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        decision = await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.now = NOW + timedelta(minutes=30)
        broker.set_mark("XLE", Decimal(102), as_of=broker.now)
        await guardian_cycle(store, broker, now=broker.now)
        backup_path = tmp_path / "restored.db"
        with sqlite3.connect(db.path) as source, sqlite3.connect(backup_path) as backup:
            source.backup(backup)
        costs = fee_events(store, broker, amount=".10")
        modeled = CostItem(
            recorded_at_utc=broker.now,
            fill_id=broker.fills[0].fill_id,
            amount=Decimal(3),
            source="MODELED",
            component="MODELED_SLIPPAGE",
            document_id="synthetic-modeled-3",
            effective_start=NOW,
            effective_end=broker.now,
        )
        modeled_event = costs[0].model_copy(
            update={
                "cost": modeled,
                "event_id": modeled.id,
                "raw_digest": content_id(modeled),
            }
        )
        subsequent = (*costs, modeled_event)
        import_broker_events(store, subsequent)
        trip = process_outcomes(store, cutoff=broker.now).outcomes[0]
        assert trip.gross_pnl == Decimal(10)
        assert trip.net_pnl == Decimal("9.8") and trip.modeled_net_pnl == Decimal(7)
        with Database(backup_path) as restored:
            restored_store = CoreStore(restored, store.config)
            import_broker_events(restored_store, subsequent)
            recovered = process_outcomes(restored_store, cutoff=broker.now).outcomes[0]
            assert recovered == trip
            assert not import_broker_events(restored_store, subsequent)
            assert not process_outcomes(restored_store, cutoff=broker.now).outcomes
            with sqlite3.connect(backup_path) as connection:
                assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        print(
            "M2_BACKUP:"
            + json.dumps(
                {
                    "lot_id": decision.id,
                    "outcome_id": trip.id,
                    "gross": "10",
                    "net": "9.8",
                    "modeled_net": "7",
                    "integrity": "ok",
                }
            )
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_outcome_index_exposes_feature_window_before_target_materialization(tmp_path):
    from sqlalchemy import select

    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.store import table
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.now = NOW + timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        with db.transaction() as session:
            row = session.execute(select(table("core_outcome_index"))).mappings().one()
            assert row["source_start"] == "2026-10-14T13:55:00Z"
            assert row["source_end"] == "2026-10-14T14:30:00Z"
            assert row["policy_id"] == store.config.policy_id
            assert row["dataset"] == "EXPLICIT_SYNTHETIC"
            assert row["evidence_lane"] == "SYNTHETIC_TEST"
            assert "net_pnl" not in row
    finally:
        db.close()


@pytest.mark.asyncio
async def test_older_receipt_cannot_branch_an_existing_cost_revision(tmp_path):
    from quantbot.paper_core.outcomes import import_broker_events
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.storage import StateConflictError

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        first = fee_events(store, broker)
        import_broker_events(store, first)
        previous = {event.cost.fill_id: event.cost.id for event in first}
        broker.now = NOW + timedelta(hours=2)
        import_broker_events(
            store, fee_events(store, broker, amount=".1", revision=2, supersedes=previous)
        )
        broker.now = NOW + timedelta(hours=1)
        with pytest.raises(StateConflictError, match="revision"):
            import_broker_events(
                store, fee_events(store, broker, amount=".2", revision=2, supersedes=previous)
            )
        assert len(store.documents("core_cost_items")) == 2
    finally:
        db.close()


@pytest.mark.parametrize("version", [1, 26, 27, 28, 29, 30])
def test_fixture_legacy_migration_keeps_numeric_zero_unknown_and_online_backup(tmp_path, version):
    from alembic import command
    from sqlalchemy import create_engine

    from quantbot.storage import Database, MigrationRequiredError, StorageRepository

    path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        command.upgrade(Database._alembic_config(connection), f"{version:04d}")
        connection.exec_driver_sql("UPDATE kill_switch_state SET reason='manual pause', engaged=1")
        connection.exec_driver_sql(
            "INSERT INTO order_intents (intent_id,client_order_id,strategy_id,signal_date,"
            "symbol,side,order_type,quantity,limit_price,stop_price,time_in_force,"
            "extended_hours,state,created_at) VALUES "
            "('i','c','s','2026-10-14','XLE','BUY','MARKET',"
            "'1',NULL,NULL,'DAY',0,'FILLED','2026-10-14T14:00:00Z')"
        )
        connection.exec_driver_sql(
            "INSERT INTO broker_orders VALUES ('o','c','XLE','BUY','MARKET','DAY','1','1',"
            "'filled','2026-10-14T14:00:00Z','100')"
        )
        connection.exec_driver_sql(
            "INSERT INTO fills VALUES ('f','o','XLE','BUY','1','100','2026-10-14T14:00:00Z','0')"
        )
    engine.dispose()
    with pytest.raises(MigrationRequiredError):
        Database(path)
    with sqlite3.connect(path) as original, sqlite3.connect(tmp_path / "backup.db") as backup:
        original.backup(backup)
    with Database.migrate(path) as db:
        with db.transaction() as session:
            repo = StorageRepository(session)
            assert repo.get_fill("f").fee is None
            assert repo.get_fill("f").fee_source == "UNKNOWN"
            assert not repo.record_fill(repo.get_fill("f"))
            assert repo.get_kill_switch_state().engaged
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT fee FROM fills").fetchone()[0] == "0"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with Database.migrate(tmp_path / "backup.db") as restored:
        with restored.transaction() as session:
            assert StorageRepository(session).get_fill("f").fee is None


@pytest.mark.asyncio
async def test_populated_accepted_m1_v30_migration_preserves_all_document_ids_and_payloads(
    tmp_path,
):
    from pathlib import Path

    from alembic import command
    from sqlalchemy import create_engine

    from quantbot.paper_core.contracts import (
        CoreConfig,
        Decision,
        ProtectionPlan,
        ReconciliationProof,
        RoundTrip,
    )
    from quantbot.paper_core.store import CoreStore
    from quantbot.research.manifest import content_id
    from quantbot.storage import Database, StateConflictError

    # This allowlisted JSON is a read-only export of the accepted M1 synthetic
    # v30 fixture; no operator state, broker transport or original DB is opened.
    fixture = json.loads((Path(__file__).parents[2] / "fixtures" / "core_v30_m1.json").read_text())
    path = tmp_path / "populated-v30.db"
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        command.upgrade(Database._alembic_config(connection), "0030")
        for name, rows in fixture.items():
            if name == "kill_switch_state":
                connection.exec_driver_sql("DELETE FROM kill_switch_state")
            for row in rows:
                keys = tuple(row)
                connection.exec_driver_sql(
                    f"INSERT INTO {name} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                    tuple(row[k] for k in keys),
                )
    engine.dispose()
    with Database.migrate(path) as db:
        config = CoreConfig.model_validate_json(fixture["core_identities"][0]["payload"])
        store = CoreStore(db, config)
        for name, kind in (
            ("core_decisions", Decision),
            ("core_protection_plans", ProtectionPlan),
            ("core_reconciliation_proofs", ReconciliationProof),
            ("core_round_trips", RoundTrip),
        ):
            for row in fixture[name]:
                record = store.read(name, row["id"], kind)
                assert record.id == content_id(json.loads(row["payload"]))
        with sqlite3.connect(path) as connection:
            for name, rows in fixture.items():
                if name.startswith("core_") and rows and "payload" in rows[0] and "id" in rows[0]:
                    stored = {
                        row[0]: row[1]
                        for row in connection.execute(f"SELECT id,payload FROM {name}")
                    }
                    assert stored == {row["id"]: row["payload"] for row in rows}
        revisions = store.documents("core_outcome_revisions")
        assert len(revisions) == 1
        assert revisions[0] == json.loads(fixture["core_round_trips"][0]["payload"])
        with pytest.raises(StateConflictError, match="REVOKED"):
            store.latest_proof()
        from quantbot.domain import Account, BrokerOrder, Fill, Position
        from quantbot.paper_core.contracts import ModelArtifact, Observation
        from quantbot.paper_core.guardian import guardian_cycle
        from quantbot.paper_core.outcomes import import_broker_events, process_outcomes
        from quantbot.paper_core.simulation import SyntheticBroker
        from quantbot.paper_core.trader import trader_cycle

        baseline = json.loads(fixture["core_fixture_baselines"][0]["payload"])
        old_proofs = [
            ReconciliationProof.model_validate_json(row["payload"])
            for row in fixture["core_reconciliation_proofs"]
        ]
        latest = max(old_proofs, key=lambda proof: proof.generation)
        broker = SyntheticBroker(
            account_id=config.account_id,
            now=latest.recorded_at_utc,
            initial_account=Account.model_validate(baseline["account"]),
            initial_positions=tuple(Position.model_validate(p) for p in baseline["positions"]),
        )
        broker.cash = latest.account.cash
        broker.quantities = dict(latest.actual_quantities)
        broker.orders = {
            row["client_order_id"]: BrokerOrder.model_validate(row)
            for row in fixture["broker_orders"]
        }
        broker.fills = [Fill.model_validate(row) for row in fixture["fills"]]
        original_decision = Decision.model_validate_json(fixture["core_decisions"][0]["payload"])
        observation = store.read("core_observations", original_decision.observation_id, Observation)
        replay = await trader_cycle(
            store, broker, observation, ModelArtifact(), old_proofs[0], now=broker.now
        )
        assert replay == original_decision and not broker.posts
        assert len(store.documents("core_decisions")) == 1
        await guardian_cycle(store, broker, now=broker.now)
        import_broker_events(store, fee_events(store, broker))
        completed = process_outcomes(store, cutoff=broker.now).outcomes[0]
        assert completed.net_pnl == Decimal("-10") and not broker.posts
        assert len(store.documents("core_outcome_revisions")) == 3
        assert json.loads(fixture["core_round_trips"][0]["payload"]) in store.documents(
            "core_outcome_revisions"
        )
