"""Scheduled adaptive entry authority and source identity, without real orders."""

from datetime import timedelta
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from quantbot.learner.experiment_contract import DecisionRecord
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.forward_source import _archive
from quantbot.research.manifest import content_id
from tests.unit.learner.test_experiment_paper import (
    cohort,
)
from tests.unit.learner.test_experiment_paper import (
    runtime_context as _pool_fixture,
)


def module():
    return import_module("quantbot.learner.experiment_entry")


def test_active_preparation_is_before_authoritative_coordinator_journal():
    from inspect import signature

    from quantbot.learner import experiment_launch as launch

    assert "prepare_active" in signature(launch._run_once).parameters
    assert callable(module().prepare_active_tick)


@pytest.fixture
async def pool_context(tmp_path):
    async for value in _pool_fixture.__wrapped__(tmp_path):
        yield value


def test_entry_configuration_cannot_broaden_risk_or_worker_count(tmp_path):
    from quantbot.learner.paper_runner import PaperExecutionPolicy

    base = dict(
        source_config=tmp_path / "shadow.json",
        source_config_hash="a" * 64,
        model_id="b" * 64,
        package_id="c" * 64,
        binding_hash="d" * 64,
        policy=PaperExecutionPolicy(),
    )
    with pytest.raises(ValueError, match="BASELINE"):
        module().EntryLaunchConfig(**base)
    with pytest.raises(ValueError):
        module().EntryLaunchConfig(**(base | {"mode": "LIVE"}))


def test_cohort_reader_uses_exact_asof_latest_activated_model(tmp_path):
    frozen, candidate, decisions, _sources = cohort()
    store = ExperimentStore(tmp_path / "e.db", create=True)
    store.append(frozen, idempotency_key="manifest")
    store.append(candidate, idempotency_key="candidate")
    for row in decisions:
        store.append(row, idempotency_key=row.decision_id)
    selected, rows = module().current_cohort(
        store, frozen, model_id=candidate.model_id, now=decisions[0].recorded_at
    )
    assert selected == candidate and rows == decisions
    selected, rows = module().current_cohort(
        store,
        frozen,
        model_id=candidate.model_id,
        now=decisions[0].features.feature_at + timedelta(seconds=121),
    )
    assert selected == candidate and rows == ()


def test_cohort_reader_does_not_substitute_challenger_or_future_decision(tmp_path):
    frozen, candidate, decisions, _sources = cohort()
    store = ExperimentStore(tmp_path / "e.db", create=True)
    store.append(frozen, idempotency_key="manifest")
    store.append(candidate, idempotency_key="candidate")
    store.append(decisions[0], idempotency_key="future")
    with pytest.raises(ValueError, match="EXACT_CANDIDATE"):
        module().current_cohort(store, frozen, model_id="e" * 64, now=decisions[0].recorded_at)
    _, rows = module().current_cohort(
        store,
        frozen,
        model_id=candidate.model_id,
        now=decisions[0].recorded_at - timedelta(seconds=1),
    )
    assert rows == ()


@pytest.fixture
def short_archive_root():
    # The two content hashes make this path long; never depend on suite basetemp.
    with TemporaryDirectory(prefix="ea-archive-") as directory:
        yield Path(directory)


def test_archive_reader_refuses_hash_substitution_and_missing_original(short_archive_root):
    frozen, _candidate, decisions, sources = cohort()
    root = short_archive_root / "shadow"
    source = sources[decisions[0].features.symbol]
    identity = frozen.roster[0]
    directory = root / "market" / content_id({"asset_id": identity.asset_id})
    _archive(directory, source)
    result = module().read_sources(root, frozen, (decisions[0],))
    assert result == {identity.symbol: source}
    path = directory / "market" / "snapshots" / (source.identity + ".json")
    path.write_text(source.model_copy(update={"empty_reason": "INCOMPLETE_DATA"}).model_dump_json())
    with pytest.raises(ValueError):
        module().read_sources(root, frozen, (decisions[0],))
    path.unlink()
    with pytest.raises(ValueError, match="SOURCE"):
        module().read_sources(root, frozen, (decisions[0],))


@pytest.mark.parametrize(
    "rows,allowed",
    [
        (
            [
                {"name": "Elrond DIA Learner", "enabled": False, "state": "Disabled"},
                {"name": "Elrond Adaptive Paper Shadow", "enabled": False, "state": "Disabled"},
            ],
            True,
        ),
        ([{"name": "Elrond DIA Learner", "enabled": True, "state": "Ready"}], False),
        ([{"name": "Elrond DIA Learner", "enabled": False, "state": "Running"}], False),
        ([], False),
    ],
)
def test_exclusive_ownership_requires_disabled_and_not_running_predecessors(rows, allowed):
    assert module().entry_ownership_safe(rows) is allowed


def test_risk_config_changes_only_roster_and_tightens_two_entry_limits():
    from quantbot.strategy import load_strategy_config

    frozen, _candidate, _decisions, _sources = cohort()
    base = load_strategy_config("config/strategy-v1.yaml")
    planned = module().entry_risk_config(base, frozen)
    assert set(planned.universe) == {row.symbol for row in frozen.roster} | {"SPY"}
    assert planned.roster_size == 1
    assert planned.risk_per_trade_bps <= base.risk_per_trade_bps
    assert planned.max_position_value_bps <= base.max_position_value_bps
    assert planned.model_dump(
        exclude={"universe", "roster_size", "risk_per_trade_bps", "max_position_value_bps"}
    ) == base.model_dump(
        exclude={"universe", "roster_size", "risk_per_trade_bps", "max_position_value_bps"}
    )


def test_forecast_selection_refuses_duplicate_symbol(tmp_path):
    frozen, candidate, decisions, _sources = cohort()
    store = ExperimentStore(tmp_path / "e.db", create=True)
    store.append(frozen, idempotency_key="manifest")
    store.append(candidate, idempotency_key="candidate")
    store.append(decisions[0], idempotency_key="first")
    duplicate = DecisionRecord.model_validate(decisions[0].model_dump() | {"reason": "duplicate"})
    store.append(duplicate, idempotency_key="second")
    with pytest.raises(ValueError, match="DUPLICATE"):
        module().current_cohort(
            store, frozen, model_id=candidate.model_id, now=decisions[0].recorded_at
        )


async def test_unactivated_model_cannot_construct_operational_runtime(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from quantbot.learner.experiment_launch import ExperimentLaunchConfig
    from tests.unit.learner.test_experiment_launch import config_data

    frozen, candidate, _decisions, _sources = cohort()
    frozen = frozen.model_copy(update={"synthetic": False})
    source = ExperimentLaunchConfig(**config_data(tmp_path))
    source.root.mkdir()
    store = ExperimentStore(source.store, create=True)
    store.append(frozen, idempotency_key="manifest")
    entry = module()
    monkeypatch.setattr(entry, "load_manifest", lambda source: frozen)
    monkeypatch.setattr(entry, "verify_checkout", lambda source: None)
    monkeypatch.setattr(entry, "clock_healthy", lambda path, now: True)
    config = SimpleNamespace(model_id=candidate.model_id)
    from tests.integration.test_execution_coordinator import _settings

    result = await entry.prepare_active_tick(
        config, source, _settings(), store, frozen, None, (), frozen.registered_at
    )
    assert result is None
    import json

    assert (
        json.loads((source.root / "entry-receipt.json").read_text())["reason"]
        == "EXACT_ARTIFACT_ACTIVATION_REQUIRED"
    )
    assert store.pending_intents(frozen.experiment_id) == ()


async def test_live_settings_refused_before_manifest_or_runtime(tmp_path):
    from quantbot.domain import BrokerEnvironment, TradingMode
    from tests.integration.test_execution_coordinator import _settings

    settings = _settings().model_copy(
        update={
            "TRADING_MODE": TradingMode.LIVE,
            "BROKER_ENVIRONMENT": BrokerEnvironment.LIVE,
        }
    )
    from types import SimpleNamespace

    result = await module().prepare_active_tick(
        None,
        SimpleNamespace(root=tmp_path),
        settings,
        None,
        None,
        None,
        (),
        cohort()[2][0].recorded_at,
    )
    assert result is None
    import json

    assert (
        json.loads((tmp_path / "entry-receipt.json").read_text())["reason"]
        == "PAPER_ONLY_CONFIGURATION_REQUIRED"
    )


def test_archive_key_cannot_escape_pinned_source_tree(tmp_path):
    frozen, _candidate, decisions, _sources = cohort()
    changed = decisions[0].model_copy(
        update={
            "features": decisions[0].features.model_copy(
                update={"source_snapshot_hash": "../../outside"}
            )
        }
    )
    with pytest.raises(ValueError, match="SOURCE_HASH"):
        module().read_sources(tmp_path, frozen, (changed,))


def test_coordinator_callback_runs_in_same_tick_without_extra_worker(tmp_path, monkeypatch):
    from quantbot.learner import experiment_launch as launch
    from tests.unit.learner.test_experiment_launch import config_data

    config = launch.ExperimentLaunchConfig(**config_data(tmp_path))
    path = tmp_path / "shadow.json"
    path.write_text(config.model_dump_json())
    monkeypatch.setattr(launch, "load_manifest", lambda config: cohort()[0])
    monkeypatch.setattr(launch, "verify_checkout", lambda config: None)
    events = []

    async def tick(config, settings):
        events.append("observation")
        return dict(
            status="NORMAL_WAIT",
            reason="MARKET_CLOSED",
            next_expected_event="SESSION",
            observations_recorded=0,
            labels_recorded=0,
            fits=0,
            intents=0,
        )

    async def entry(config, settings):
        events.append("entry")

    monkeypatch.setattr(launch, "run_once", tick)
    assert launch.run_experiment(path, once=True, after_tick=entry) == 0
    assert events == ["observation", "entry"]


def test_task_plan_is_pinned_limited_and_has_no_other_worker():
    from xml.etree import ElementTree as ET

    document = module().task_xml(
        repository=r"D:\Elrond\releases\candidate",
        config=r"D:\Elrond\config\entry.json",
        config_hash="f" * 64,
        host=r"D:\Elrond\host.exe",
        log=r"D:\Elrond\entry.log",
        user_id="existing-operator",
        commit="a" * 40,
    )
    task = ET.fromstring(document)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert (
        task.findtext("t:RegistrationInfo/t:URI", namespaces=ns) == "\\Elrond Adaptive PAPER Entry"
    )
    assert task.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=ns) == "LeastPrivilege"
    assert task.findtext("t:Settings/t:WakeToRun", namespaces=ns) == "false"
    assert task.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=ns) == "PT0S"
    arguments = task.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns)
    assert "start-adaptive-entry.ps1" in arguments
    assert "-ConfigHash " + "f" * 64 in arguments
    assert "five_minute_launch" not in arguments and "--env-file" not in arguments


def test_native_entry_launcher_uses_retained_job_and_exact_config_pin():
    from pathlib import Path

    path = Path("deploy/omen/start-adaptive-entry.ps1")
    assert path.is_file(), "Missing supervised active native launcher"
    source = path.read_text()
    assert "New-ElrondProcessJob" in source
    assert "quantbot.learner.experiment_entry" in source
    assert "'--config-hash',$ConfigHash" in source
    assert "Register-ScheduledTask" not in source
    assert "RunAs" not in source


@pytest.mark.parametrize("status", ["FAULTED", "CLOCK_SYNC_REQUIRED", "LEASE_BUSY"])
def test_failed_observation_cannot_dispatch_current_or_cached_entry(tmp_path, monkeypatch, status):
    from quantbot.learner import experiment_launch as launch
    from tests.unit.learner.test_experiment_launch import config_data

    config = launch.ExperimentLaunchConfig(**config_data(tmp_path))
    path = tmp_path / "shadow.json"
    path.write_text(config.model_dump_json())
    monkeypatch.setattr(launch, "load_manifest", lambda config: cohort()[0])
    monkeypatch.setattr(launch, "verify_checkout", lambda config: None)

    async def tick(config, settings):
        return dict(
            status=status,
            reason="synthetic failure",
            next_expected_event="RECOVERY",
            observations_recorded=0,
            labels_recorded=0,
            fits=0,
            intents=0,
        )

    async def entry(config, settings):
        pytest.fail("Failed coordinator invoked entry")

    monkeypatch.setattr(launch, "run_once", tick)
    launch.run_experiment(path, once=True, after_tick=entry)


def test_runtime_cannot_bootstrap_missing_base_identity_or_risk_policy(tmp_path):
    from quantbot.risk.policy_versions import active_policy_version
    from quantbot.storage import Database, StorageRepository
    from quantbot.strategy import StrategyConfig, build_strategy_identity
    from tests.integration.test_runtime_wiring import _config_payload

    base = StrategyConfig(**_config_payload())
    db = Database.create(tmp_path / "op.db")
    before = db.path.read_bytes()
    assert not module().runtime_baseline_verified(db.path, base)
    assert db.path.read_bytes() == before
    with db.transaction() as session:
        repository = StorageRepository(session)
        repository.save_strategy_deployment(
            build_strategy_identity(
                base, git_commit="a" * 40, deployment_timestamp=cohort()[2][0].recorded_at
            )
        )
    assert not module().runtime_baseline_verified(db.path, base)
    with db.transaction() as session:
        active_policy_version(StorageRepository(session), now=cohort()[2][0].recorded_at)
    assert module().runtime_baseline_verified(db.path, base)
    db.close()


@pytest.mark.parametrize("mode", ["entry", "protected", "recovery", "unsent"])
async def test_prepared_context_creates_one_authoritative_activated_decision(
    pool_context, tmp_path, monkeypatch, mode
):
    """Fabricated auth/provider fixtures test graph wiring, never real activation."""
    from types import SimpleNamespace

    from quantbot.learner import experiment_activation, experiment_coordinator
    from quantbot.learner.experiment_launch import ExperimentLaunchConfig
    from quantbot.learner.experiment_observation import build_observation_batch
    from quantbot.learner.experiment_research import FitReceipt
    from quantbot.learner.five_minute_paper import FiveMinuteModelView
    from quantbot.learner.paper_runner import IntradayPaperServices
    from quantbot.operations.cycle import CycleResult
    from quantbot.storage import Database
    from tests.integration.test_execution_coordinator import _settings
    from tests.unit.learner.test_experiment_launch import config_data
    from tests.unit.learner.test_experiment_paper import _commissioned_pool

    protected = mode == "protected"
    recovery = mode in {"recovery", "unsent"}
    kwargs, broker, runner, supervisor = await _commissioned_pool(pool_context)
    # False synthetic fields exist ONLY in this isolated test to exercise the
    # real-route conditional; auth and cycle edges remain explicitly fabricated.
    frozen = kwargs["manifest"].model_copy(update={"synthetic": False})
    candidate = kwargs["candidate"].model_copy(
        update={
            "synthetic": False,
            "manifest_hash": content_id(frozen),
        }
    )
    source = ExperimentLaunchConfig(
        **(
            config_data(tmp_path)
            | {
                "operations_database": kwargs["database"].path,
                "code_commit": kwargs["identity"].git_commit,
            }
        )
    )
    source.root.mkdir()
    guard = Database.create(source.root / "guard.db")
    guard.close()
    store = ExperimentStore(source.store, create=True)
    store.append(frozen, idempotency_key="manifest")
    store.append(candidate, idempotency_key="candidate")
    store.append(
        FitReceipt(
            experiment_id=frozen.experiment_id,
            recorded_at=frozen.registered_at,
            request_id="a" * 64,
            trial_id="FABRICATED_FIXTURE_NO_FIT_CALL",
            status="SUCCEEDED",
            charged=True,
            candidate_id=candidate.model_id,
            reason="TEST_FIXTURE_ONLY",
            synthetic=False,
        ),
        idempotency_key="fixture-fit-receipt",
    )
    pending = (
        [SimpleNamespace(state="PENDING" if mode == "unsent" else "CLAIMED")] if recovery else []
    )
    if recovery:
        monkeypatch.setattr(store, "pending_intents", lambda *args: tuple(pending))
        for original in kwargs["decisions"]:
            historical = original.model_copy(update={"model_id": candidate.model_id})
            store.append(historical, idempotency_key="old:" + historical.decision_id)
    capture = next(iter(kwargs["sources"].values()))
    observation = build_observation_batch(frozen, capture, now=broker.now)
    runner.model = FiveMinuteModelView(
        candidate.model_id, tuple(row.symbol for row in frozen.roster)
    )
    config = module().EntryLaunchConfig(
        source_config=tmp_path / "shadow.json",
        source_config_hash="a" * 64,
        model_id=candidate.model_id,
        package_id="c" * 64,
        binding_hash=kwargs["binding"].configuration_hash,
        policy=kwargs["policy"],
    )
    activation = SimpleNamespace(
        model_id=config.model_id,
        package_id=config.package_id,
        binding_hash=config.binding_hash,
        activation_id="f" * 64,  # fabricated digest, not an approval
        synthetic=False,
        recorded_at=frozen.registered_at,
    )
    monkeypatch.setattr(module(), "active_artifact", lambda *args, **kw: activation)
    monkeypatch.setattr(experiment_activation, "active_artifact", lambda *args, **kw: activation)
    monkeypatch.setattr(module(), "clock_healthy", lambda path, now: True)
    monkeypatch.setattr(module(), "read_entry_ownership", lambda: True)
    monkeypatch.setattr(module(), "_guard", lambda *args: not protected)
    monkeypatch.setattr(
        module(), "read_sources", lambda *args: pytest.fail("Current snapshots were not reused")
    )
    closed = []
    if recovery:
        monkeypatch.setattr(module(), "read_sources", lambda *args: kwargs["sources"])

    async def close():
        closed.append("closed")

    runtime = SimpleNamespace(
        database=kwargs["database"],
        settings=_settings(),
        config=kwargs["config"],
        running_git_commit=kwargs["identity"].git_commit,
        aclose=close,
    )
    monkeypatch.setattr(
        "quantbot.runtime.runtime_paths",
        lambda: SimpleNamespace(database=kwargs["database"].path, config=tmp_path / "base.yaml"),
    )
    monkeypatch.setattr("quantbot.runtime.build_runtime", lambda settings: runtime)
    monkeypatch.setattr(module(), "load_strategy_config", lambda path: runtime.config)
    monkeypatch.setattr(module(), "runtime_baseline_verified", lambda *args: True)
    monkeypatch.setattr(
        module(),
        "build_pooled_paper_services",
        lambda *args, **kw: IntradayPaperServices(
            runner=runner, deadlines=supervisor, cycle=SimpleNamespace(_data_sync=SimpleNamespace())
        ),
    )
    prepared = await module().prepare_active_tick(
        config,
        source,
        _settings(),
        store,
        frozen,
        None if recovery else observation,
        () if recovery else (capture,),
        broker.now,
    )
    if protected:
        assert prepared is None and not closed and not broker.submitted
        return
    assert prepared is not None
    assert (prepared.context is None) == recovery
    calls = []
    recovered = []

    async def dispatch(*args, **kw):
        recovered.append(kw["services"])
        if mode != "unsent":
            pending.clear()
        return ()

    if recovery:
        monkeypatch.setattr("quantbot.learner.experiment_execution.dispatch_pending", dispatch)

    async def cycle(*args, **kw):
        calls.append(kw["decisions"])
        return CycleResult(
            run_id=kw["run_id"],
            status="HALTED",
            reasons=("FABRICATED_CYCLE_BOUNDARY",),
            signals_evaluated=0,
            orders_submitted=0,
            started_at=broker.now,
            finished_at=broker.now,
        )

    monkeypatch.setattr("quantbot.learner.experiment_execution.run_experiment_cycle", cycle)
    monkeypatch.setattr(
        "quantbot.learner.experiment_lot_reader.refresh_lot_journal", lambda *args, **kw: ()
    )
    result = await experiment_coordinator.coordinator_tick(
        store,
        frozen,
        now=broker.now,
        observation=None if recovery else observation,
        execution=prepared.services,
        active_cycle=prepared.context,
        clock_verified=True,
        owner="fixture-coordinator",
    )
    decisions = [
        event.decode(DecisionRecord)
        for event in store.read_as_of(frozen.experiment_id, broker.now)
        if event.kind == "DecisionRecord"
    ]
    if recovery:
        assert recovered == [prepared.services]
        assert bool(pending) == (mode == "unsent")
        if mode == "unsent":
            import json

            receipt = json.loads((source.root / "entry-receipt.json").read_text())
            assert receipt["status"] == "OPERATOR_REVIEW"
            assert receipt["reason"] == "PENDING_UNSENT_OPERATOR_REVIEW_REQUIRED"
        assert not calls and result.decisions_recorded == 0 and not broker.submitted
        await prepared.close()
        assert closed == ["closed"]
        return
    assert result.decisions_recorded == len(observation.features)
    assert len(decisions) == len(observation.features)
    assert len({row.features.symbol for row in decisions}) == len(decisions)
    assert all(row.activation_id == activation.activation_id for row in decisions)
    assert calls and all(row.paper_eligible for row in calls[0])
    assert not broker.submitted
    await prepared.close()
    assert closed == ["closed"]


def test_wrong_legacy_entry_alias_cannot_satisfy_transfer():
    assert not module().entry_ownership_safe(
        [
            {"name": "Elrond Five Minute PAPER Entry", "enabled": False, "state": "Disabled"},
            {"name": "Elrond Adaptive Paper Shadow", "enabled": False, "state": "Disabled"},
        ]
    )


def test_native_ownership_query_names_actual_entry_owner(monkeypatch):
    from types import SimpleNamespace

    def query(command, **kwargs):
        script = command[-1]
        assert "Elrond DIA Learner" in script
        assert "Elrond Five Minute PAPER Entry" not in script
        assert "Elrond Adaptive Paper Shadow" in script
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '[{"name":"Elrond DIA Learner","enabled":false,"state":"Disabled"},'
                '{"name":"Elrond Adaptive Paper Shadow","enabled":false,"state":"Disabled"}]'
            ),
        )

    monkeypatch.setattr(module().subprocess, "run", query)
    assert module().read_entry_ownership()
