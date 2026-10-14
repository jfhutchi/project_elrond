"""Fifty deterministic round trips are capacity evidence, never a trade quota."""

import asyncio
import json
import subprocess
import time
from datetime import timedelta

import pytest


@pytest.mark.asyncio
async def test_fifty_synthetic_round_trips_and_guardian_progress_during_fit(tmp_path, monkeypatch):
    from tests.fixtures.core_m4 import ExecutionClock, episodes, learning_calendar

    from quantbot.paper_core import learning
    from quantbot.paper_core.activation import (
        ActivationAuthority,
        activate_model,
        commission_fixture_authority,
        initialize_fixture_model,
    )
    from quantbot.paper_core.contracts import LearningPolicy, ModelArtifact, Observation
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, parent, trips, joins = await episodes(tmp_path, 50)
    try:
        assert len(trips) == 50 and len({t.lot_id for t in trips}) == 50
        assert len(broker.posts) == 100 and len(store.documents("core_round_trips")) == 50
        # One separately identified open lot is protected while the actual child
        # holds its worker lock and has entered the fixed numerical fitting path.
        template = Observation.model_validate(joins[0]["observation"])
        now = broker.now.replace(hour=19, minute=55)
        broker.now = now
        broker.set_mark("XLE", template.price, as_of=now)
        await guardian_cycle(store, broker, now=now)
        observation = template.model_copy(
            update={
                "source_start": now - timedelta(minutes=35),
                "source_end": now,
                "recorded_at_utc": now,
                "available_at": now,
                "dependency_available_at": (now,),
                "sequence": 1001,
                "raw_data_digest": "capacity-owned-during-fit",
                "session_close": now.replace(hour=20, minute=0),
            }
        )
        collect = ModelArtifact(feature_version="five-minute-price-alpha-v1")
        protected = await trader_cycle(
            store, broker, observation, collect, store.latest_proof(), now=now
        )
        assert protected.action == "BUY"
        cutoff = now.replace(hour=20, minute=0)
        ready, resume = tmp_path / "fit-ready", tmp_path / "fit-resume"
        original_run = subprocess.run
        child_code = """
import sys,time
from pathlib import Path
from quantbot.paper_core import learning, learning_procedure
source,target,ready,resume=sys.argv[1:]
original=learning_procedure.fit_candidate
def slow_fit(*args,**kwargs):
    Path(ready).write_text('actual-worker-lock-and-fit-path')
    while not Path(resume).exists(): time.sleep(.01)
    return original(*args,**kwargs)
learning_procedure.fit_candidate=slow_fit
sys.argv=[sys.argv[0],source,target]
learning._main()
"""

        def delayed_child(command, *args, **kwargs):
            if command[3:5] == ["-m", "quantbot.paper_core.learning"]:
                command = [*command[:3], "-c", child_code, *command[5:], str(ready), str(resume)]
            return original_run(command, *args, **kwargs)

        monkeypatch.setattr(learning.subprocess, "run", delayed_child)
        fit = asyncio.create_task(
            asyncio.to_thread(
                learning.consider_learning,
                store,
                LearningPolicy(parent_model=parent),
                cutoff=cutoff,
                calendar=learning_calendar(),
                execution_clock=ExecutionClock(cutoff),
            )
        )
        deadline = time.monotonic() + 15
        while not ready.exists():
            assert time.monotonic() < deadline, "fit child did not reach actual fit path"
            await asyncio.sleep(0.01)
        broker.now = cutoff + timedelta(seconds=1)
        setup_started = time.monotonic()
        try:
            from quantbot.paper_core.store import CoreStore
            from quantbot.storage import Database

            with Database(db.path) as restarted:
                restarted_store = CoreStore(restarted, store.config)
                assert len(restarted_store.links()) == 101
                restart_setup_seconds = time.monotonic() - setup_started
                progress_started = time.monotonic()
                await guardian_cycle(restarted_store, broker, now=broker.now)
                # guardian_cycle already processes outcomes; read the durable
                # result rather than repeat the entire historical resolution.
                progressed_outcomes = restarted_store.documents("core_round_trips")
                progress_seconds = time.monotonic() - progress_started
            total_intervention_seconds = time.monotonic() - setup_started
            print(
                "M5_CAPACITY_PROGRESS:"
                + json.dumps(
                    {
                        "restart_setup_seconds": restart_setup_seconds,
                        "guardian_and_durable_outcome_seconds": progress_seconds,
                        "total_intervention_seconds": total_intervention_seconds,
                        "guardian_progress_bound_seconds": 10,
                        "duplicate_outcome_resolution": False,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            assert progress_seconds < 10
            assert any(t["decision_id"] == protected.id for t in progressed_outcomes)
            assert broker.quantities["XLE"] == 0
        finally:
            resume.write_text("resume fixed fit")
        receipt = await fit
        assert receipt.status == "AWAITING_ACTIVATION", receipt.reason
        assert len(receipt.final_input_ids) == 50
        from quantbot.paper_core.contracts import ModelArtifact

        candidate = store.read("core_models", receipt.resulting_model_id, ModelArtifact)
        genesis = initialize_fixture_model(store, parent, at=broker.now)
        authority = ActivationAuthority.fixture(
            store,
            candidate_id=candidate.id,
            receipt_id=receipt.id,
            expected_activation_id=genesis.id,
            at=broker.now,
        )
        commission_fixture_authority(store, authority)
        activation = activate_model(
            store,
            expected_model_id=parent.id,
            candidate_id=candidate.id,
            authority=authority,
            at=broker.now,
        )
        later_at = broker.now.replace(hour=14, minute=5) + timedelta(days=1)
        broker.now = later_at
        broker.set_mark("XLE", template.price, as_of=later_at)
        await guardian_cycle(store, broker, now=later_at)
        later_observation = template.model_copy(
            update={
                "recorded_at_utc": later_at,
                "source_start": later_at - timedelta(minutes=35),
                "source_end": later_at,
                "available_at": later_at,
                "dependency_available_at": (later_at,),
                "sequence": 1002,
                "session_close": later_at.replace(hour=20, minute=0),
                "raw_data_digest": "capacity-later-model-use",
            }
        )
        later_decision = await trader_cycle(
            store, broker, later_observation, parent, store.latest_proof(), now=later_at
        )
        assert later_decision.model_id == later_decision.model_digest == candidate.id
        assert later_decision.learning_receipt_id == receipt.id
        assert later_decision.activation_id == activation.id
        # The concurrent protection lot has unknown fees, so this later actual
        # model acknowledgement truthfully waits for independent cash authority.
        assert later_decision.action == "WAIT"
        print(
            "M5_CAPACITY:"
            + json.dumps(
                {
                    "lane": "SYNTHETIC_TEST",
                    "deterministic_capacity_round_trips": 50,
                    "natural_daily_throughput_claimed": False,
                    "capacity_lot_ids": [t.lot_id for t in trips],
                    "capacity_outcome_ids": [t.id for t in trips],
                    "session_count": 10,
                    "concurrent_child_worker_lock": True,
                    "restart_preserved_claims": True,
                    "guardian_outcome_progress_seconds": progress_seconds,
                    "restart_setup_seconds": restart_setup_seconds,
                    "total_intervention_seconds": total_intervention_seconds,
                    "progress_bound_seconds": 10,
                    "poll_seconds": 0.01,
                    "separate_protected_decision_id": protected.id,
                    "protected_chain": store.export_loop(protected.id),
                    "fit_receipt": receipt.model_dump(mode="json"),
                    "activation": activation.model_dump(mode="json"),
                    "later_actual_decision": later_decision.model_dump(mode="json"),
                },
                sort_keys=True,
            )
        )
    finally:
        db.close()
