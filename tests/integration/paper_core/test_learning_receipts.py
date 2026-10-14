"""Actual outcome-to-shadow numerical fit and predecessor migration witnesses."""

import json
import sqlite3
from datetime import timedelta

import pytest
from tests.fixtures.core_m4 import ExecutionClock, episodes, learning_calendar


@pytest.mark.asyncio
async def test_actual_costed_episodes_fit_and_export(tmp_path, monkeypatch):
    from quantbot.paper_core.contracts import LearningPolicy, ModelArtifact
    from quantbot.paper_core.learning import consider_learning, predict_bps
    from quantbot.research.registry import HypothesisRegistry

    db, store, broker, parent, trips, joins = await episodes(tmp_path)
    try:
        from quantbot.paper_core import learning
        from quantbot.paper_core.store import CoreStore
        from quantbot.storage import Database

        before = tmp_path / "before-fit.db"
        with sqlite3.connect(db.path) as source, sqlite3.connect(before) as destination:
            source.backup(destination)
        cutoff = broker.now + timedelta(hours=8)
        policy = LearningPolicy(
            parent_model=parent,
            procedure_version="daily-all-history-walk-forward-v2",
            max_sessions=None,
        )
        calendar = learning_calendar()
        clock = ExecutionClock(cutoff)
        receipt = consider_learning(
            store, policy, cutoff=cutoff, calendar=calendar, execution_clock=clock
        )
        assert receipt.status == "AWAITING_ACTIVATION", receipt.reason
        assert receipt.activation_at is None
        assert receipt.parent_model_id == parent.id != receipt.resulting_model_id
        assert len(receipt.training_ids) == 30 and len(receipt.validation_ids) == 10
        assert len(receipt.session_metrics) == 5
        assert len(receipt.final_input_ids) == 40
        assert [len(f.training_ids) for f in receipt.fold_results] == [30, 32, 34, 36, 38]
        assert all(f.scores.incumbent_bps == (0, 0) for f in receipt.fold_results)
        candidate = store.read("core_models", receipt.resulting_model_id, ModelArtifact)
        assert candidate.learning_receipt_id == receipt.run_id
        assert any(c != 0 for c in candidate.estimator.coefficients)
        assert predict_bps(candidate, (dict(joins[0]["observation"]["features"]),))[0] > 10
        assert (
            consider_learning(
                store, policy, cutoff=cutoff, calendar=calendar, execution_clock=clock
            )
            == receipt
        )
        with db.transaction() as session:
            assert HypothesisRegistry(session).learner_trials_spent() == 1
        # Reach the later cutoff in the declared execution domain; otherwise
        # the accepted future-cutoff guard must refuse before budget admission.
        clock.advance(1)
        repeat = consider_learning(
            store,
            policy,
            cutoff=cutoff + timedelta(seconds=1),
            calendar=calendar,
            execution_clock=clock,
        )
        assert repeat.status == "NO_CHANGE" and repeat.reason == "SESSION_BUDGET_OR_INPUT_REUSED"
        assert repeat.trial_charge_id is None
        failure_path = tmp_path / "failed-fit.db"
        with sqlite3.connect(before) as source, sqlite3.connect(failure_path) as destination:
            source.backup(destination)
        original_fit = learning._fit_subprocess

        def failed(*args, **kwargs):
            with failure_db.transaction() as session:
                assert HypothesisRegistry(session).learner_trials_spent() == 1
            raise TimeoutError("synthetic child failed")

        with Database(failure_path) as failure_db:
            failure_store = CoreStore(failure_db, store.config)
            monkeypatch.setattr(learning, "_fit_subprocess", failed)
            failure = consider_learning(
                failure_store, policy, cutoff=cutoff, calendar=calendar, execution_clock=clock
            )
            assert failure.status == "FIT_FAILED" and failure.reason == "TimeoutError"
            assert failure.resulting_model_id == parent.id and failure.activation_at is None
            assert (
                consider_learning(
                    failure_store, policy, cutoff=cutoff, calendar=calendar, execution_clock=clock
                )
                == failure
            )
            assert (
                consider_learning(
                    failure_store,
                    policy,
                    cutoff=cutoff + timedelta(seconds=1),
                    calendar=calendar,
                    execution_clock=clock,
                ).status
                == "NO_CHANGE"
            )
            monkeypatch.setattr(learning, "_fit_subprocess", original_fit)
        from tests.fixtures.core_m4_process import exercise_process_recovery

        process_evidence = await exercise_process_recovery(
            tmp_path, before, store.config, policy, cutoff, receipt, store, broker, joins
        )
        print("M4_PROCESSES:" + json.dumps(process_evidence, sort_keys=True))
        print("M4_FIT_FAILED:" + failure.model_dump_json())
        # A late actual fee changes the revision, never the number of trade samples.
        from quantbot.paper_core.contracts import BrokerEvent, CostItem
        from quantbot.paper_core.outcomes import import_broker_events, process_outcomes
        from quantbot.research.manifest import content_id

        old_inputs = candidate.input_ids
        correction_at = broker.now + timedelta(hours=8)
        clock.advance((correction_at - clock.at).total_seconds())
        old = next(
            CostItem.model_validate(c)
            for c in joins[0]["cost_items"]
            if c["fill_id"] == trips[0].entry_fill_ids[0]
        )
        revised = old.model_copy(
            update={"recorded_at_utc": correction_at, "revision": 2, "supersedes": old.id}
        )
        # Decimal accounting is explicit, including the late revision fixture.
        from decimal import Decimal

        revised = revised.model_copy(update={"amount": Decimal(".01")})
        order = next(
            o
            for o in broker.orders.values()
            if o.broker_order_id == broker.fills[0].broker_order_id
        )
        import_broker_events(
            store,
            (
                BrokerEvent(
                    recorded_at_utc=correction_at,
                    occurred_at=correction_at,
                    account_id=store.config.account_id,
                    endpoint="synthetic/fee-correction",
                    client_order_id=order.client_order_id,
                    event_id=revised.id,
                    raw_digest=content_id(revised),
                    cost=revised,
                ),
            ),
        )
        corrections = process_outcomes(store, cutoff=correction_at).outcomes
        corrected = next(t for t in corrections if t.lot_id == trips[0].lot_id)
        assert corrected.supersedes == trips[0].id
        reconsidered = consider_learning(
            store,
            policy,
            cutoff=correction_at,
            calendar=calendar,
            execution_clock=clock,
        )
        assert (
            corrected.id in reconsidered.considered_ids
            and trips[0].id not in reconsidered.considered_ids
        )
        assert reconsidered.status == "AWAITING_ACTIVATION", reconsidered.reason
        assert corrected.id in reconsidered.final_input_ids
        assert all(
            trips[0].id in f.training_ids and corrected.id not in f.training_ids
            for f in reconsidered.fold_results
        )
        assert candidate.input_ids == old_inputs and trips[0].id in old_inputs
        assert len(store.documents("core_round_trips")) == 40
        print(
            "M4_CORRECTION:"
            + json.dumps(
                {
                    "old_input_ids": old_inputs,
                    "corrected": corrected.model_dump(mode="json"),
                    "receipt": reconsidered.model_dump(mode="json"),
                    "sample_count": 40,
                    "calendar": calendar.model_dump(mode="json"),
                    "candidate": store.read(
                        "core_models", reconsidered.resulting_model_id, ModelArtifact
                    ).model_dump(mode="json"),
                    "export": next(
                        p
                        for p in store.documents("core_learning_inputs")
                        if content_id(p) == reconsidered.input_export_id
                    ),
                    "joins": [store.export_loop(j["decision_id"]) for j in joins],
                },
                sort_keys=True,
            )
        )
        print(
            "M4_JOINED:"
            + json.dumps(
                {
                    "receipt": receipt.model_dump(mode="json"),
                    "parent": parent.model_dump(mode="json"),
                    "candidate": candidate.model_dump(mode="json"),
                    "joins": joins,
                    "calendar": calendar.model_dump(mode="json"),
                    "export": next(
                        p
                        for p in store.documents("core_learning_inputs")
                        if content_id(p) == receipt.input_export_id
                    ),
                    "fixture_parent_benchmark_bps": -20,
                    "units": "net_pnl/entry_notional * 10000 = bps",
                    "positive_prediction_bps": predict_bps(
                        candidate, (dict(joins[0]["observation"]["features"]),)
                    )[0],
                },
                sort_keys=True,
            )
        )
        quiet_at = calendar.sequence.sessions[11].close_at
        clock.advance((quiet_at - clock.at).total_seconds())
        quiet = consider_learning(
            store, policy, cutoff=quiet_at, calendar=calendar, execution_clock=clock
        )
        assert quiet.status == "NO_CHANGE" and quiet.trial_charge_id is None
        assert quiet.reason == "SESSION_BUDGET_OR_INPUT_REUSED"
        with db.transaction() as session:
            assert HypothesisRegistry(session).learner_trials_spent() == 2
        print("M4_DAILY_ACTUAL_QUIET:" + quiet.model_dump_json())
    finally:
        db.close()


@pytest.mark.asyncio
async def test_populated_accepted_v32_migration_preserves_payload_ids_and_claim_replay(tmp_path):
    from pathlib import Path

    from alembic import command
    from sqlalchemy import create_engine

    from quantbot.domain import Account, BrokerOrder, Fill, Position
    from quantbot.paper_core.contracts import (
        CoreConfig,
        Decision,
        ModelArtifact,
        Observation,
        ProtectionPlan,
        ReconciliationProof,
        RoundTrip,
    )
    from quantbot.paper_core.simulation import SyntheticBroker
    from quantbot.paper_core.store import CoreStore
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.research.manifest import content_id
    from quantbot.storage import Database, MigrationRequiredError

    fixture = json.loads((Path(__file__).parents[2] / "fixtures/core_v32_m3.json").read_text())
    path = tmp_path / "accepted-v32.db"
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        command.upgrade(Database._alembic_config(connection), "0032")
        for name, rows in fixture.items():
            if name == "core_fixture_models":
                continue
            if name == "kill_switch_state":
                connection.exec_driver_sql("DELETE FROM kill_switch_state")
            for row in rows:
                keys = tuple(row)
                connection.exec_driver_sql(
                    f"INSERT INTO {name} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                    tuple(row[k] for k in keys),
                )
    engine.dispose()
    before = path.read_bytes()
    with pytest.raises(MigrationRequiredError):
        Database(path)
    assert path.read_bytes() == before
    with Database.migrate(path) as db:
        config = CoreConfig.model_validate_json(fixture["core_identities"][0]["payload"])
        store = CoreStore(db, config)
        for name, kind in (
            ("core_observations", Observation),
            ("core_decisions", Decision),
            ("core_protection_plans", ProtectionPlan),
            ("core_reconciliation_proofs", ReconciliationProof),
            ("core_outcome_revisions", RoundTrip),
        ):
            for row in fixture[name]:
                record = store.read(name, row["id"], kind)
                assert record.id == row["id"] == content_id(json.loads(row["payload"]))
        model_row = fixture["core_fixture_models"][0]
        assert (
            ModelArtifact.model_validate_json(model_row["payload"]).id
            == model_row["id"]
            == ModelArtifact().id
        )
        with sqlite3.connect(path) as connection:
            for name, rows in fixture.items():
                if (
                    name != "core_fixture_models"
                    and name.startswith("core_")
                    and "payload" in rows[0]
                    and "id" in rows[0]
                ):
                    assert dict(connection.execute(f"SELECT id,payload FROM {name}")) == {
                        r["id"]: r["payload"] for r in rows
                    }
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        baseline = json.loads(fixture["core_fixture_baselines"][0]["payload"])
        proof = ReconciliationProof.model_validate_json(
            fixture["core_reconciliation_proofs"][0]["payload"]
        )
        broker = SyntheticBroker(
            account_id=config.account_id,
            now=proof.recorded_at_utc,
            initial_account=Account.model_validate(baseline["account"]),
            initial_positions=tuple(Position.model_validate(p) for p in baseline["positions"]),
        )
        broker.orders = {
            r["client_order_id"]: BrokerOrder.model_validate(r) for r in fixture["broker_orders"]
        }
        broker.fills = [Fill.model_validate(r) for r in fixture["fills"]]
        original = Decision.model_validate_json(fixture["core_decisions"][0]["payload"])
        observation = store.read("core_observations", original.observation_id, Observation)
        replay = await trader_cycle(
            store, broker, observation, ModelArtifact(), proof, now=broker.now
        )
        assert replay == original and not broker.posts
        assert replay.time_projection is not None


@pytest.mark.asyncio
async def test_truthful_no_change(tmp_path):
    from quantbot.paper_core.contracts import LearningPolicy
    from quantbot.paper_core.learning import consider_learning

    db, store, broker, parent, trips, joins = await episodes(tmp_path, 2)
    try:
        receipt = consider_learning(
            store,
            LearningPolicy(parent_model=parent),
            cutoff=broker.now + timedelta(hours=8),
            execution_clock=ExecutionClock(broker.now + timedelta(hours=8)),
            calendar=learning_calendar(),
        )
        assert receipt.status == "NO_CHANGE" and receipt.reason == "TEN_COMPLETED_SESSION_WARMUP"
        assert receipt.parent_model_id == receipt.resulting_model_id == parent.id
        assert receipt.activation_at is None and receipt.trial_charge_id is None
        assert set(receipt.considered_ids) == {t.id for t in trips}
        print(
            "M4_NO_CHANGE:"
            + json.dumps(
                {"receipt": receipt.model_dump(mode="json"), "joins": joins}, sort_keys=True
            )
        )
    finally:
        db.close()
