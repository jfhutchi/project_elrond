"""Pinned shadow launch cannot silently activate trading or broaden providers."""

from importlib import import_module
from pathlib import Path

import pytest

from tests.fixtures.adaptive_paper import manifest


def module():
    return import_module("quantbot.learner.experiment_launch")


def config_data(tmp_path):
    return dict(
        root=tmp_path / "shadow",
        store=tmp_path / "shadow" / "experiment.db",
        manifest_path=tmp_path / "manifest.json",
        operations_database=tmp_path / "operations.db",
        declaration_database=tmp_path / "declarations.db",
        clock_receipt=tmp_path / "clock.json",
        code_commit="a" * 40,
        manifest_hash=module().content_id(manifest()),
        mode="SHADOW",
        trading_enabled=False,
        maximum_workers=2,
    )


def test_config_cannot_reenable_entries_or_change_worker_budget(tmp_path):
    base = config_data(tmp_path)
    config = module().ExperimentLaunchConfig(**base)
    assert config.mode == "SHADOW"
    for changes in ({"trading_enabled": True}, {"mode": "LIVE"}, {"maximum_workers": 3}):
        with pytest.raises(ValueError):
            module().ExperimentLaunchConfig(**(base | changes))


@pytest.mark.parametrize("configured_research", [False, True])
def test_observational_loop_starts_no_unconfigured_research_worker(
    tmp_path, monkeypatch, configured_research
):
    import asyncio
    from types import SimpleNamespace

    launch = module()
    data = config_data(tmp_path)
    if configured_research:
        data["research_sources"] = dict(
            training_path=tmp_path / "training.json",
            validation_path=tmp_path / "validation.json",
            training_hash="a" * 64,
            validation_hash="b" * 64,
        )
    config = launch.ExperimentLaunchConfig(**data)
    starts = []
    ticks = []
    real_sleep = asyncio.sleep

    class StopLoop(BaseException):
        pass

    async def held_worker(function, worker_config):
        starts.append((function, worker_config))
        await asyncio.Event().wait()

    async def tick(tick_config, settings):
        ticks.append(tick_config)
        await real_sleep(0)
        return dict(
            status="NORMAL_WAIT",
            reason="SESSION_CLOSED",
            next_expected_event="SESSION_OPEN",
            observations_recorded=0,
            labels_recorded=0,
            fits=0,
            intents=0,
        )

    async def sleep(seconds):
        assert seconds == 120
        if len(ticks) == 2:
            raise StopLoop
        await real_sleep(0)

    monkeypatch.setattr(launch, "_read_json", lambda path: config.model_dump(mode="python"))
    monkeypatch.setattr(launch, "load_manifest", lambda value: manifest())
    monkeypatch.setattr(launch, "verify_checkout", lambda value: None)
    monkeypatch.setattr(
        launch,
        "Settings",
        lambda: SimpleNamespace(
            TRADING_MODE=SimpleNamespace(value="PAPER"),
            BROKER_ENVIRONMENT=SimpleNamespace(value="PAPER"),
        ),
    )
    monkeypatch.setattr(launch, "run_once", tick)
    monkeypatch.setattr(launch.asyncio, "to_thread", held_worker)
    monkeypatch.setattr(launch.asyncio, "sleep", sleep)
    with pytest.raises(StopLoop):
        launch.run_experiment(tmp_path / "config.json")
    assert len(ticks) == 2
    assert starts == ([(launch.run_research_once, config)] if configured_research else [])


def test_output_cannot_overlap_authoritative_sources(tmp_path):
    base = config_data(tmp_path)
    with pytest.raises(ValueError):
        module().ExperimentLaunchConfig(**(base | {"operations_database": base["store"]}))
    with pytest.raises(ValueError):
        module().ExperimentLaunchConfig(**(base | {"root": Path("relative")}))


def test_manifest_pin_and_synthetic_real_boundary_checked(tmp_path):
    base = config_data(tmp_path)
    base["manifest_path"].write_text(manifest().model_dump_json())
    config = module().ExperimentLaunchConfig(**base)
    with pytest.raises(ValueError, match="SYNTHETIC_NOT_DEPLOYABLE"):
        module().load_manifest(config)
    config = config.model_copy(update={"manifest_hash": "b" * 64})
    with pytest.raises(ValueError, match="MANIFEST_PIN"):
        module().load_manifest(config)


@pytest.mark.parametrize(
    "failure,reason",
    [
        (ValueError("CLOCK_SYNC_REQUIRED"), "CLOCK_SYNC_REQUIRED"),
        (ValueError("private credential-like diagnostic"), "COORDINATOR_TICK_FAILED"),
        (TimeoutError(), "COORDINATOR_TICK_TIMEOUT"),
    ],
)
async def test_runtime_failure_is_bounded_visible_and_sanitized(
    tmp_path, monkeypatch, failure, reason
):
    launch = module()
    config = launch.ExperimentLaunchConfig(**config_data(tmp_path))
    monkeypatch.setattr(launch, "load_manifest", lambda config: manifest())
    monkeypatch.setattr(launch, "verify_checkout", lambda config: None)

    async def fail(config, settings):
        raise failure

    monkeypatch.setattr(launch, "_run_once", fail, raising=False)
    result = await launch.run_once(config, object())
    assert result["reason"] == reason
    assert result["status"] == (
        "CLOCK_SYNC_REQUIRED" if reason == "CLOCK_SYNC_REQUIRED" else "FAULTED"
    )
    assert result["clock_verified"] is False
    assert result["fits"] == result["intents"] == 0
    saved = (config.root / "receipt.json").read_text()
    assert "private credential" not in saved
    assert "SHADOW" in saved


async def test_cancellation_is_not_misreported_as_recoverable_failure(tmp_path, monkeypatch):
    import asyncio

    launch = module()
    config = launch.ExperimentLaunchConfig(**config_data(tmp_path))
    monkeypatch.setattr(launch, "load_manifest", lambda config: manifest())
    monkeypatch.setattr(launch, "verify_checkout", lambda config: None)

    async def cancel(config, settings):
        raise asyncio.CancelledError()

    monkeypatch.setattr(launch, "_run_once", cancel, raising=False)
    with pytest.raises(asyncio.CancelledError):
        await launch.run_once(config, object())
    assert not (config.root / "receipt.json").exists()


@pytest.mark.parametrize("clock_ok", [True, False])
async def test_operational_writer_contention_is_wait_not_research_fault(
    tmp_path, monkeypatch, clock_ok,
):
    from quantbot.operations.cycle import OperationalWriterBusy
    launch = module()
    config = launch.ExperimentLaunchConfig(**config_data(tmp_path))
    monkeypatch.setattr(launch, "load_manifest", lambda config: manifest())
    monkeypatch.setattr(launch, "verify_checkout", lambda config: None)
    monkeypatch.setattr(launch, "clock_healthy", lambda *args: clock_ok)
    async def busy(*args, **kwargs):
        raise OperationalWriterBusy("private path")
    monkeypatch.setattr(launch, "_run_once", busy)
    result = await launch.run_once(config, object())
    assert result['status'] == ('NORMAL_WAIT' if clock_ok else 'CLOCK_SYNC_REQUIRED')
    assert result['reason'] == ('OPERATIONAL_WRITER_BUSY' if clock_ok else 'CLOCK_SYNC_REQUIRED')
    assert result['clock_verified'] is clock_ok
    assert result['intents'] == 0
    assert 'private path' not in (config.root/'receipt.json').read_text()


@pytest.mark.parametrize('stamp,seconds', [
    ('2026-10-08T17:24:50+00:00',30),
    ('2026-10-08T17:25:05+00:00',15),
    ('2026-10-08T17:25:20+00:00',120),
    ('2026-10-08T17:23:00+00:00',120),
])
def test_natural_poll_aligns_to_bar_publication_without_widening_age(stamp, seconds):
    from datetime import datetime
    delay = getattr(module(), 'cohort_poll_delay', None)
    assert callable(delay)
    assert delay(datetime.fromisoformat(stamp)) == seconds


def test_research_sources_are_all_or_none_and_cannot_overlap_output(tmp_path):
    launch = module()
    base = config_data(tmp_path)
    source = dict(
        training_path=tmp_path / "training.json",
        validation_path=tmp_path / "validation.json",
        training_hash="a" * 64,
        validation_hash="b" * 64,
    )
    config = launch.ExperimentLaunchConfig(**(base | {"research_sources": source}))
    assert config.research_sources.training_hash == "a" * 64
    with pytest.raises(ValueError):
        launch.ExperimentLaunchConfig(
            **(base | {"research_sources": {"training_path": source["training_path"]}})
        )
    with pytest.raises(ValueError, match="OVERLAP"):
        launch.ExperimentLaunchConfig(
            **(base | {"research_sources": source | {"training_path": base["store"]}})
        )


def test_unverified_worker_clock_prevents_store_or_registry_access(tmp_path, monkeypatch):
    launch = module()
    config = launch.ExperimentLaunchConfig(**config_data(tmp_path))
    monkeypatch.setattr(launch, "load_manifest", lambda config: manifest())
    monkeypatch.setattr(launch, "verify_checkout", lambda config: None)
    monkeypatch.setattr(launch, "clock_healthy", lambda path, now: False)
    result = launch.run_research_once(config)
    assert result["status"] == "CLOCK_SYNC_REQUIRED"
    assert not config.store.exists()


def test_missing_predeclared_inputs_is_setup_wait_not_model_approval(tmp_path, monkeypatch):
    launch = module()
    config = launch.ExperimentLaunchConfig(**config_data(tmp_path))
    monkeypatch.setattr(launch, "load_manifest", lambda config: manifest())
    monkeypatch.setattr(launch, "verify_checkout", lambda config: None)
    monkeypatch.setattr(launch, "clock_healthy", lambda path, now: True)
    result = launch.run_research_once(config)
    assert result["status"] == "NORMAL_WAIT"
    assert result["reason"] == "PREDECLARED_RESEARCH_SOURCES_REQUIRED"
    assert not config.store.exists()
    saved = (config.root / "worker-receipt.json").read_text()
    assert "PREDECLARED_RESEARCH_SOURCES_REQUIRED" in saved


def test_worker_failure_has_durable_sanitized_receipt_not_silent_pause(tmp_path, monkeypatch):
    launch = module()
    config = launch.ExperimentLaunchConfig(**config_data(tmp_path))
    monkeypatch.setattr(launch, "load_manifest", lambda config: manifest())
    monkeypatch.setattr(launch, "verify_checkout", lambda config: None)

    def fail(config):
        import json

        running = json.loads((config.root / "worker-receipt.json").read_text())
        assert running["status"] == "STARTED"
        raise RuntimeError("private credential-bearing registry details")

    monkeypatch.setattr(launch, "_run_research_once", fail, raising=False)
    result = launch.run_research_once(config)
    assert result["status"] == "FAULTED"
    saved = (config.root / "worker-receipt.json").read_text()
    assert "RESEARCH_WORKER_FAILED" in saved
    assert "private credential" not in saved
    assert not config.store.exists()


def test_task_config_change_is_refused_before_any_runtime_or_manifest_access(tmp_path, monkeypatch):
    launch = module()
    path = tmp_path / "changed.json"
    path.write_text("{}")
    with pytest.raises(ValueError, match="PINNED_LAUNCH_CONFIGURATION_CHANGED"):
        launch.run_experiment(path, check=True, config_hash="a" * 64)


async def test_guard_covers_actual_exchange_day_query_before_provider(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    launch = module()
    actual = datetime(2026, 10, 7, 2, tzinfo=UTC)
    guarded = []
    calls = []

    def guard(database, start, end):
        guarded.append((start, end))
        return start.date() != datetime(2026, 10, 6).date()

    async def collect(*args, **kwargs):
        calls.append(args)
        raise AssertionError("protected query reached provider")

    monkeypatch.setattr(launch, "_guard", guard)
    monkeypatch.setattr(launch, "collect_snapshot", collect)
    with pytest.raises(ValueError, match="PROTECTED_OBSERVATION_WINDOW"):
        await launch._collect_guarded_day(
            object(),
            tmp_path,
            ("DIA",),
            actual.astimezone(launch.XNYS_TIMEZONE).date(),
            object(),
            clock=lambda: actual,
        )
    assert guarded[0][0] == datetime(2026, 10, 6, tzinfo=UTC)
    assert guarded[0][1].date() == guarded[0][0].date()
    assert not calls


def test_pending_target_days_survive_overnight_restart(tmp_path):
    from datetime import timedelta

    from quantbot.learner.experiment_observation import build_observation_batch
    from quantbot.learner.experiment_store import ExperimentStore
    from tests.fixtures.adaptive_paper import OPEN, snapshot

    store = ExperimentStore(tmp_path / "pending.db", create=True)
    store.append(manifest(), idempotency_key="manifest")
    source = snapshot()
    batch = build_observation_batch(manifest(), source, now=source.received_at)
    store.append(batch, idempotency_key="observation")
    now = OPEN + timedelta(days=1)
    assert module()._pending_label_days(store, manifest(), now=now) == (OPEN.date(),)


def test_pending_recovery_rotates_missing_dates_instead_of_starving_later_days(tmp_path):
    from datetime import timedelta

    from quantbot.learner.experiment_observation import build_observation_batch
    from quantbot.learner.experiment_store import ExperimentStore
    from tests.fixtures.adaptive_paper import OPEN, snapshot

    store = ExperimentStore(tmp_path / "fair.db", create=True)
    store.append(manifest(), idempotency_key="manifest")
    for n in range(3):
        source = snapshot(opened=OPEN + timedelta(days=n))
        store.append(
            build_observation_batch(manifest(), source, now=source.received_at),
            idempotency_key=f"observation:{n}",
        )
    now = OPEN + timedelta(days=4)
    launch = module()
    first = launch._pending_label_days(store, manifest(), now=now)
    assert first == (OPEN.date(), (OPEN + timedelta(days=1)).date())
    for day in first:
        attempt = launch.PendingLabelRecovery(
            experiment_id=manifest().experiment_id,
            recorded_at=now,
            session_date=day,
            status="UNAVAILABLE",
            synthetic=True,
        )
        store.append(attempt, idempotency_key="attempt:" + day.isoformat())
    assert launch._pending_label_days(store, manifest(), now=now) == (
        (OPEN + timedelta(days=2)).date(),
        OPEN.date(),
    )


@pytest.mark.parametrize("history_failure", [False, True])
async def test_backlog_before_near_expired_current_feature_and_failure_is_isolated(
    tmp_path, monkeypatch, history_failure
):
    from datetime import datetime, timedelta

    from quantbot.learner.estimators import FittedEstimator
    from quantbot.learner.experiment_contract import CandidateArtifact
    from quantbot.learner.experiment_observation import build_observation_batch
    from quantbot.learner.experiment_store import ExperimentStore
    from tests.fixtures.adaptive_paper import OPEN, snapshot

    launch = module()
    config = launch.ExperimentLaunchConfig(**config_data(tmp_path))
    config.root.mkdir()
    store = ExperimentStore(config.store, create=True)
    store.append(manifest(), idempotency_key="manifest")
    old = snapshot()
    store.append(
        build_observation_batch(manifest(), old, now=old.received_at), idempotency_key="old"
    )
    candidate = CandidateArtifact(
        experiment_id=manifest().experiment_id,
        recorded_at=OPEN - timedelta(seconds=1),
        manifest_hash=launch.content_id(manifest()),
        trial_id="synthetic",
        training_source_hash="a" * 64,
        validation_source_hash="b" * 64,
        synthetic=True,
        artifact=FittedEstimator(
            family="mean",
            feature_names=manifest().feature_names,
            means=(0.0,) * 10,
            scales=(1.0,) * 10,
            coefficients=(0.0,) * 10,
            intercept=0.02,
            alpha=1.0,
            training_mse=0.0,
            seed=0,
            algorithm_version="mean-v1",
        ),
    )
    store.append(candidate, idempotency_key="candidate")
    clock = [OPEN + timedelta(days=1, minutes=35, seconds=119)]
    calls = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    def received(source):
        return source.model_copy(
            update={
                "received_at": clock[0],
                "inputs": tuple(
                    row.model_copy(update={"available_at": clock[0]}) for row in source.inputs
                ),
                "receipts": tuple(
                    row.model_copy(update={"received_at": clock[0]}) for row in source.receipts
                ),
            }
        )

    async def history(*args, **kwargs):
        calls.append("history")
        clock[0] += timedelta(seconds=2)
        if history_failure:
            raise RuntimeError("synthetic unavailable prior target")
        return received(snapshot(65))

    async def current(manifest, **kwargs):
        calls.append("current")
        source = received(snapshot(opened=OPEN + timedelta(days=1)))
        return build_observation_batch(manifest, source, now=clock[0])

    monkeypatch.setattr(launch, "datetime", Clock)
    monkeypatch.setattr(launch, "clock_healthy", lambda path, now: True)
    monkeypatch.setattr(launch, "load_manifest", lambda config: manifest())
    monkeypatch.setattr(launch, "verify_checkout", lambda config: None)
    monkeypatch.setattr(launch, "_collect_guarded_day", history)
    monkeypatch.setattr(launch, "collect_observations", current)
    # Lot-reader acceptance is independently tested; no operational source here.
    monkeypatch.setattr(
        "quantbot.learner.experiment_lot_reader.refresh_lot_journal", lambda *args, **kwargs: ()
    )
    result = await launch._run_once(config, object())
    assert calls == ["history", "current"]
    assert result["status"] != "FAULTED"
    assert result["decisions_recorded"] == 0
    assert result["labels_recorded"] == (0 if history_failure else 6)
    events = store.read_as_of(manifest().experiment_id, clock[0])
    attempts = [
        row.decode(launch.PendingLabelRecovery)
        for row in events
        if row.kind == "PendingLabelRecovery"
    ]
    assert len(attempts) == 1
    assert attempts[0].status == ("UNAVAILABLE" if history_failure else "FETCHED")
