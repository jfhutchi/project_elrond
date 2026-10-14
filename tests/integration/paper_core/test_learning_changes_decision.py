"""Removing atomic activation or runtime pointer use must break the later BUY proof."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest


@pytest.mark.asyncio
async def test_accepted_daily_refit_changes_same_later_observation(tmp_path):
    from tests.fixtures.core_m5 import accepted

    from quantbot.paper_core import activation
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, parent, candidate, receipt, observation = accepted(tmp_path)
    try:
        genesis = activation.initialize_fixture_model(store, parent, at=broker.now)
        await guardian_cycle(store, broker, now=broker.now)
        proof = store.latest_proof()
        before = await trader_cycle(store, broker, observation, parent, proof, now=broker.now)
        assert before.action == "WAIT" and "PREDICT_NET_RESERVE" in before.gates
        authority = activation.ActivationAuthority.fixture(
            store,
            candidate_id=candidate.id,
            receipt_id=receipt.id,
            expected_activation_id=genesis.id,
            at=broker.now,
        )
        activation.commission_fixture_authority(store, authority)
        event = activation.activate_model(
            store,
            expected_model_id=parent.id,
            candidate_id=candidate.id,
            authority=authority,
            at=broker.now,
        )
        # The retained caller-supplied parent cannot substitute for the active pointer.
        after = await trader_cycle(store, broker, observation, parent, proof, now=broker.now)
        assert before.observation_id == after.observation_id
        assert before.inventory_id == after.inventory_id
        assert after.action == "BUY" and not after.gates, after.gates
        assert after.model_id == after.model_digest == candidate.id
        assert after.learning_receipt_id == receipt.id
        assert after.activation_id == event.id and after.model_generation == 2
        assert 10 < after.prediction_bps < 20
        assert len(broker.posts) == 1
        from tests.fixtures.core_m4 import ExecutionClock, learning_calendar

        from quantbot.paper_core.contracts import BrokerEvent, CostItem, LearningPolicy
        from quantbot.paper_core.learning import consider_learning
        from quantbot.paper_core.outcomes import import_broker_events, process_outcomes
        from quantbot.research.manifest import content_id

        plans = store.documents("core_protection_plans")
        original_receipt = receipt.model_dump_json()
        no_change = consider_learning(
            store,
            LearningPolicy(parent_model=candidate),
            cutoff=broker.now,
            calendar=learning_calendar(),
            execution_clock=ExecutionClock(broker.now),
        )
        assert no_change.status == "NO_CHANGE" and no_change.resulting_model_id == candidate.id
        ack_authority = activation.ActivationAuthority.fixture(
            store,
            candidate_id=candidate.id,
            receipt_id=no_change.id,
            expected_activation_id=event.id,
            at=broker.now,
        )
        activation.commission_fixture_authority(store, ack_authority)
        ack = activation.activate_model(
            store,
            expected_model_id=candidate.id,
            candidate_id=candidate.id,
            authority=ack_authority,
            at=broker.now,
        )
        assert ack.action == "ACKNOWLEDGE" and ack.model_id == event.model_id
        # Entry model/protection stays immutable while the pointer acknowledges a receipt.
        assert store.documents("core_protection_plans") == plans
        broker.now += timedelta(minutes=30)
        broker.set_mark("XLE", Decimal("100.2"), as_of=broker.now)
        await guardian_cycle(store, broker, now=broker.now)
        costs = []
        for fill in broker.fills[-2:]:
            cost = CostItem(
                recorded_at_utc=broker.now,
                fill_id=fill.fill_id,
                amount=0,
                source="BROKER_REPORTED",
                document_id="m5-synthetic-zero-" + fill.fill_id,
                effective_start=observation.available_at,
                effective_end=broker.now,
            )
            order = next(
                o for o in broker.orders.values() if o.broker_order_id == fill.broker_order_id
            )
            costs.append(
                BrokerEvent(
                    recorded_at_utc=broker.now,
                    occurred_at=broker.now,
                    account_id=store.config.account_id,
                    endpoint="synthetic/fees",
                    client_order_id=order.client_order_id,
                    event_id=cost.id,
                    raw_digest=content_id(cost),
                    cost=cost,
                )
            )
        import_broker_events(store, tuple(costs))
        (trip,) = process_outcomes(store, cutoff=broker.now).outcomes
        assert trip.decision_id == after.id and trip.net_pnl / trip.entry_notional == Decimal(
            ".002"
        )
        assert trip.status == "COMPLETE" and broker.quantities["XLE"] == 0
        from quantbot.paper_core.guardian import synchronize

        await synchronize(store, broker, now=broker.now)
        await guardian_cycle(store, broker, now=broker.now)
        fresh = observation.model_copy(
            update={
                "sequence": 1001,
                "recorded_at_utc": broker.now,
                "available_at": broker.now,
                "source_end": broker.now,
                "source_start": broker.now - timedelta(minutes=35),
                "dependency_available_at": (broker.now,),
                "raw_data_digest": "m5-ack-later",
            }
        )
        later = await trader_cycle(
            store, broker, fresh, parent, store.latest_proof(), now=broker.now
        )
        assert later.learning_receipt_id == no_change.id and later.activation_id == ack.id
        assert later.model_id == candidate.id and later.prediction_bps == after.prediction_bps
        history = (store.documents("core_outcome_revisions"), store.documents("core_learning_runs"))
        rolled = activation.rollback_model(
            store,
            expected_model_id=candidate.id,
            target_id=parent.id,
            expected_activation_id=ack.id,
            reason="reviewed local model rollback",
            at=broker.now,
        )
        assert rolled.generation == 4 and activation.read_active_model(store).id == parent.id
        assert history == (
            store.documents("core_outcome_revisions"),
            store.documents("core_learning_runs"),
        )
        assert receipt.model_dump_json() == original_receipt and receipt.activation_at is None
        # Rollback cannot orphan the later candidate-owned open lot.
        retained = store.read("core_decisions", later.id, type(later))
        broker.now += timedelta(minutes=30)
        broker.set_mark("XLE", Decimal("100.2"), as_of=broker.now)
        await guardian_cycle(store, broker, now=broker.now)
        assert broker.quantities["XLE"] == 0
        assert store.read("core_decisions", later.id, type(later)) == retained
        assert retained.model_id == candidate.id and retained.activation_id == ack.id
        later_outcomes = store.documents("core_round_trips")
        assert any(
            t["decision_id"] == later.id and Decimal(t["open_quantity"]) == 0
            for t in later_outcomes
        )
        bundle = activation.export_loop_evidence(store, decision_id=after.id)
        print(
            "M5_ACTIVATION:"
            + json.dumps(
                {
                    "before": before.model_dump(mode="json"),
                    "after": after.model_dump(mode="json"),
                    "later_ack_decision": later.model_dump(mode="json"),
                    "later_protection_after_rollback": store.export_loop(later.id),
                    "compatibility_lineage": {
                        "accepted_bootstrap_source": "fbdfca23f74bec146d19c630f001786c1123b2a6",
                        "transition": "BOUNDED_COLLECT_TO_PREDICT",
                        "fixed_feature_target_exit_risk_envelope": True,
                        "original_bootstrap_input_ids": list(candidate.input_ids),
                        "code_only_maintenance_preserves_original_ids": True,
                        "executed_selected_outcomes_are_unbiased_opportunities": False,
                    },
                    "bundle": bundle,
                },
                sort_keys=True,
            )
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_populated_accepted_v33_migration_and_old_claim_replay(tmp_path):
    import gzip
    import sqlite3
    from pathlib import Path

    from alembic import command
    from sqlalchemy import create_engine
    from tests.fixtures.core_m5 import accepted

    from quantbot.paper_core.activation import read_active_model
    from quantbot.paper_core.contracts import Decision, LearningReceipt, ModelArtifact, Observation
    from quantbot.paper_core.store import CoreStore
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.research.manifest import content_id
    from quantbot.storage import Database, MigrationRequiredError, StateConflictError

    fixture = json.loads(
        gzip.decompress((Path(__file__).parents[2] / "fixtures/core_v33_m4.json.gz").read_bytes())
    )
    path = tmp_path / "accepted-v33.db"
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        command.upgrade(Database._alembic_config(connection), "0033")
        for name, rows in fixture.items():
            if name in {"alembic_version", "schema_version"} or not rows:
                continue
            if name == "kill_switch_state":
                connection.exec_driver_sql("DELETE FROM kill_switch_state")
            for row in rows:
                keys = tuple(row)
                connection.exec_driver_sql(
                    f'INSERT INTO "{name}" ({",".join(keys)}) VALUES '
                    f"({','.join('?' for _ in keys)})",
                    tuple(row[k] for k in keys),
                )
    engine.dispose()
    before = path.read_bytes()
    with pytest.raises(MigrationRequiredError):
        Database(path)
    assert path.read_bytes() == before
    other = tmp_path / "replay"
    other.mkdir()
    replay_db, _, broker, _, _, _, _ = accepted(other)
    replay_db.close()
    from quantbot.paper_core.contracts import CoreConfig

    config = CoreConfig.model_validate_json(fixture["core_identities"][0]["payload"])
    with Database.migrate(path) as db:
        store = CoreStore(db, config)
        with pytest.raises(StateConflictError, match="UNCOMMISSIONED"):
            read_active_model(store)
        for name, kind in (
            ("core_decisions", Decision),
            ("core_models", ModelArtifact),
            ("core_learning_runs", LearningReceipt),
            ("core_observations", Observation),
        ):
            for row in fixture[name]:
                decoded = store.read(name, row["id"], kind)
                assert decoded.id == row["id"] == content_id(json.loads(row["payload"]))
        original = Decision.model_validate_json(fixture["core_decisions"][0]["payload"])
        observation = store.read("core_observations", original.observation_id, Observation)
        result = await trader_cycle(
            store, broker, observation, ModelArtifact(), store.latest_proof(), now=broker.now
        )
        assert result == original and not broker.posts
        with sqlite3.connect(path) as connection:
            for name, rows in fixture.items():
                if name not in {"alembic_version", "schema_version"} and rows:
                    connection.row_factory = sqlite3.Row
                    actual = [dict(row) for row in connection.execute(f'SELECT * FROM "{name}"')]
                    assert sorted(actual, key=lambda r: json.dumps(r, sort_keys=True)) == sorted(
                        rows, key=lambda r: json.dumps(r, sort_keys=True)
                    ), name
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            restored_path = tmp_path / "restored.db"
            with sqlite3.connect(restored_path) as destination:
                connection.backup(destination)
    with Database(restored_path) as restored:
        restored_store = CoreStore(restored, config)
        assert len(restored_store.documents("core_round_trips")) == 40
        assert not restored_store.documents("core_activations")
    print(
        "M5_V33_REPLAY:"
        + json.dumps(
            {
                "old_schema": 33,
                "new_schema": 34,
                "tables_preserved": len(fixture) - 2,
                "claimed_decision_id": original.id,
                "integrity": "ok",
                "backup_restored": True,
                "auto_activation": False,
            },
            sort_keys=True,
        )
    )
