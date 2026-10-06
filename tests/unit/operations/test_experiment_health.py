"""Component liveness remains separate from closed-market economic waiting."""

import json
from datetime import timedelta
from importlib import import_module

from quantbot.learner.experiment_coordinator import TickReceipt
from quantbot.learner.experiment_store import ExperimentStore
from tests.fixtures.adaptive_paper import manifest


def sources(tmp_path, *, status="NORMAL_WAIT", age=0):
    frozen = manifest()
    path = tmp_path / "experiment.db"
    store = ExperimentStore(path, create=True)
    store.append(frozen, idempotency_key="manifest")
    store.append(
        TickReceipt(
            experiment_id=frozen.experiment_id,
            recorded_at=frozen.registered_at,
            tick_id="a" * 64,
            input_hash="b" * 64,
            status="NORMAL_WAIT",
            reason="MARKET_CLOSED",
            next_expected_event="NEXT_EXCHANGE_SESSION",
            next_wakeup_at=frozen.registered_at + timedelta(minutes=2),
            clock_verified=True,
            synthetic=True,
        ),
        idempotency_key="tick",
    )
    worker = dict(
        experiment_id=frozen.experiment_id,
        recorded_at=(frozen.registered_at - timedelta(seconds=age)).isoformat(),
        status=status,
        reason="BOUNDED_RESEARCH_ATTEMPT_STARTED" if status == "STARTED" else "FRESH_DATA_REQUIRED",
        code_commit="c" * 40,
        scope="SHADOW",
        synthetic=True,
    )
    (tmp_path / "worker-receipt.json").write_text(json.dumps(worker))
    return path, frozen.registered_at


def health(path, now):
    return import_module("quantbot.operations.experiment_health").measure_experiment_health(
        path, expected_commit="c" * 40, now=now
    )


def test_closed_market_is_wait_but_dead_worker_is_not(tmp_path):
    path, now = sources(tmp_path)
    before = path.read_bytes()
    result = health(path, now)
    assert result["coordinator"] == "FRESH"
    assert result["worker"] == "FRESH"
    assert result["conditions"] == ()
    late = health(path, now + timedelta(minutes=5))
    assert {condition.code for condition in late["conditions"]} == {
        "EXPERIMENT_COORDINATOR_UNVERIFIED",
        "EXPERIMENT_WORKER_UNVERIFIED",
    }
    assert path.read_bytes() == before


def test_started_worker_has_bounded_fit_grace_not_indefinite_health(tmp_path):
    path, now = sources(tmp_path, status="STARTED", age=600)
    assert health(path, now)["worker"] == "RUNNING_BOUNDED"
    late = health(path, now + timedelta(minutes=8))
    assert late["worker"] == "UNVERIFIED"


def test_wrong_pin_future_or_failed_receipt_never_claims_worker_health(tmp_path):
    path, now = sources(tmp_path)
    receipt = tmp_path / "worker-receipt.json"
    original = json.loads(receipt.read_text())
    for patch in (
        {"code_commit": "d" * 40},
        {"recorded_at": (now + timedelta(seconds=1)).isoformat()},
        {"status": "FAULTED"},
    ):
        receipt.write_text(json.dumps(original | patch))
        assert health(path, now)["worker"] == "UNVERIFIED"


def test_missing_store_health_does_not_create_store(tmp_path):
    path = tmp_path / "missing.db"
    result = health(path, manifest().registered_at)
    assert result["coordinator"] == "UNVERIFIED"
    assert not path.exists()


def test_monitor_deduplicates_and_measures_recovery_without_touching_source(tmp_path):
    from quantbot.operations.monitor import run_monitor
    from tests.unit.operations.test_monitor import sources as operational_sources

    path, now = sources(tmp_path)
    operational_root = tmp_path / "operational"
    operational_root.mkdir()
    learner, database = operational_sources(operational_root, at=now)
    output = tmp_path / "alerts"
    before = database.read_bytes()
    arguments = dict(experiment_store=path, experiment_commit="c" * 40)
    late = now + timedelta(minutes=5)
    first = run_monitor(learner, database, output, now=late, **arguments)
    codes = {a["code"] for a in first["active_alerts"]}
    assert "EXPERIMENT_COORDINATOR_UNVERIFIED" in codes
    second = run_monitor(learner, database, output, now=late + timedelta(seconds=1), **arguments)
    assert not [
        event for event in second["notifications"] if event["code"].startswith("EXPERIMENT_")
    ]
    frozen = manifest()
    store = ExperimentStore(path)
    store.append(
        TickReceipt(
            experiment_id=frozen.experiment_id,
            recorded_at=late + timedelta(seconds=2),
            tick_id="d" * 64,
            input_hash="e" * 64,
            status="NORMAL_WAIT",
            reason="MARKET_CLOSED",
            next_expected_event="NEXT_EXCHANGE_SESSION",
            next_wakeup_at=late + timedelta(minutes=2),
            clock_verified=True,
            synthetic=True,
        ),
        idempotency_key="recovered-tick",
    )
    receipt = path.parent / "worker-receipt.json"
    document = json.loads(receipt.read_text())
    document["recorded_at"] = (late + timedelta(seconds=2)).isoformat()
    receipt.write_text(json.dumps(document))
    recovered = run_monitor(learner, database, output, now=late + timedelta(seconds=3), **arguments)
    assert not [a for a in recovered["active_alerts"] if a["code"].startswith("EXPERIMENT_")]
    assert database.read_bytes() == before
