"""Pinned exploration launch refuses authority expansion before runtime construction."""
from contextlib import nullcontext
from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from types import SimpleNamespace

import pytest

from quantbot.learner.paper_runner import PaperExecutionPolicy
from quantbot.strategy import load_strategy_config
from tests.unit.learner.test_paper_exploration import example


@pytest.mark.parametrize("equity,cash,age,fractional,reason", [
    ("500", "100", 0, False, "PAPER_WHOLE_SHARE_AFFORDABILITY_BLOCKED"),
    ("100000", "1", 0, False, "PAPER_WHOLE_SHARE_AFFORDABILITY_BLOCKED"),
    ("100000", "10000", 0, False, None),
    ("500", "100", 121, False, None),
    ("500", "100", -1, False, None),
    ("100", "100", 0, True, "PAPER_FRACTIONAL_MINIMUM_UNAFFORDABLE"),
    ("500", "100", 0, True, None),
])
def test_affordability_preflight_is_necessary_not_risk_approval(
    monkeypatch, equity, cash, age, fractional, reason,
):
    module = import_module("quantbot.learner.exploration_launch")
    manifest, capture, _ = example()
    from quantbot.learner.experiment_observation import build_observation_batch
    batch = build_observation_batch(manifest, capture, now=capture.received_at)
    snapshot = SimpleNamespace(captured_at=capture.received_at-timedelta(seconds=age),
                               account=SimpleNamespace(equity=Decimal(equity), cash=Decimal(cash),
                                                       buying_power=Decimal(cash)))
    repository = SimpleNamespace(get_latest_account_snapshot=lambda _:snapshot)
    monkeypatch.setattr(module, "StorageRepository", lambda _:repository)
    runtime = SimpleNamespace(database=SimpleNamespace(transaction=lambda:nullcontext(None)),
                              settings=SimpleNamespace(EXPECTED_ACCOUNT_ID="synthetic-paper"),
                              config=SimpleNamespace(max_position_value_bps=50,
                                                     allow_fractional_shares=fractional))
    result = module.affordability_objections(runtime, batch, PaperExecutionPolicy(),
                                             now=capture.received_at)
    assert result == (() if reason is None else (reason,))


def test_exploration_launch_config_requires_inherited_exit_owner(tmp_path):
    module = import_module("quantbot.learner.exploration_launch")
    base = dict(source_config=tmp_path / "source.json", source_config_hash="a" * 64,
                policy_path=tmp_path / "policy.json", policy_id="b" * 64,
                binding_hash="c" * 64, policy=PaperExecutionPolicy())
    with pytest.raises(ValueError, match="BASELINE"):
        module.ExplorationLaunchConfig(**base)
    with pytest.raises(ValueError):
        module.ExplorationLaunchConfig(**(base | {"mode": "LIVE"}))


def test_exploration_launch_only_tightens_retained_strategy_risk():
    module = import_module("quantbot.learner.exploration_launch")
    manifest, _, policy = example()
    base = load_strategy_config("config/strategy-v1.yaml")
    planned = module.exploration_risk_config(base, manifest, policy)
    assert planned.max_position_value_bps <= min(base.max_position_value_bps, 50)
    assert planned.risk_per_trade_bps <= min(base.risk_per_trade_bps, 25)
    assert planned.model_dump(exclude={"universe", "roster_size", "max_position_value_bps",
                                       "risk_per_trade_bps"}) == base.model_dump(
        exclude={"universe", "roster_size", "max_position_value_bps", "risk_per_trade_bps"})


def test_explicit_fractional_quantity_mode_changes_only_rounding():
    module = import_module("quantbot.learner.exploration_launch")
    manifest, _, policy = example()
    base = load_strategy_config("config/strategy-v1.yaml")
    whole = module.exploration_risk_config(base, manifest, policy, quantity_mode="WHOLE")
    fractional = module.exploration_risk_config(base, manifest, policy, quantity_mode="FRACTIONAL")
    assert not whole.allow_fractional_shares and fractional.allow_fractional_shares
    assert whole.model_dump(exclude={"allow_fractional_shares"}) == fractional.model_dump(
        exclude={"allow_fractional_shares"})


def test_exploration_task_plan_retains_limited_identity_and_pinned_native_launcher():
    from xml.etree import ElementTree as ET

    module = import_module("quantbot.learner.exploration_launch")
    document = module.task_xml(repository=r"C:\Elrond\release", config=r"C:\Elrond\entry.json",
        config_hash="a"*64, host=r"C:\Elrond\host.exe", log=r"C:\Elrond\worker.log",
        user_id="synthetic-local-user", commit="b"*40)
    root = ET.fromstring(document)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=ns) == "LeastPrivilege"
    assert root.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=ns) == "IgnoreNew"
    assert root.findtext("t:RegistrationInfo/t:URI", namespaces=ns) == "\\Elrond PAPER Exploration"
    arguments = root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns)
    assert arguments is not None and "-Exploration" in arguments and "a"*64 in arguments


@pytest.mark.parametrize("failure", ["synthetic", "changed-file", "wrong-policy"])
def test_policy_file_is_exact_and_never_synthetic_authority(tmp_path, failure):
    module = import_module("quantbot.learner.exploration_launch")
    _, _, policy = example()
    if failure != "synthetic":
        policy = policy.model_copy(update={"synthetic": False})
    path = tmp_path / "policy.json"
    path.write_text(policy.model_dump_json(), encoding="utf-8")
    config = type("Config", (), {"policy_path": path, "policy_id": policy.policy_id})()
    if failure == "changed-file":
        path.write_text(policy.model_copy(update={"code_hash": "d" * 64}).model_dump_json())
    elif failure == "wrong-policy":
        config.policy_id = "e" * 64
    with pytest.raises(ValueError, match="EXACT_REAL_EXPLORATION_POLICY"):
        module.read_policy(config)


def test_baseline_diagnostics_retain_negative_scores_and_replay_original(tmp_path):
    from quantbot.learner.experiment_observation import build_observation_batch
    from quantbot.learner.experiment_store import ExperimentStore
    from tests.unit.learner.test_experiment_paper import cohort

    module = import_module("quantbot.learner.exploration_launch")
    manifest, candidate, _, sources = cohort()
    candidate = candidate.model_copy(update={
        "artifact": candidate.artifact.model_copy(update={"intercept": -0.003})})
    capture = next(iter(sources.values()))
    observation = build_observation_batch(manifest, capture, now=capture.received_at)
    store = ExperimentStore(tmp_path / "diagnostics.db", create=True)
    store.append(manifest, idempotency_key="manifest")
    store.append(candidate, idempotency_key="candidate")
    rows = module.baseline_diagnostics(store, manifest, observation, now=observation.recorded_at)
    assert len(rows) == len(manifest.roster)
    assert all(row.score < 0 and row.paper_eligible is False for row in rows)
    replay = module.baseline_diagnostics(store, manifest, observation,
                                          now=observation.recorded_at+timedelta(minutes=31))
    assert replay == rows
    assert sum(event.kind == "DecisionRecord" for event in store.read_as_of(
        manifest.experiment_id, observation.recorded_at+timedelta(minutes=31))) == len(rows)


def test_historical_recovery_never_fabricates_a_new_stale_baseline(tmp_path):
    from quantbot.learner.experiment_observation import build_observation_batch
    from quantbot.learner.experiment_store import ExperimentStore
    from tests.unit.learner.test_experiment_paper import cohort

    module = import_module("quantbot.learner.exploration_launch")
    manifest, candidate, _, sources = cohort()
    capture = next(iter(sources.values()))
    observation = build_observation_batch(manifest, capture, now=capture.received_at)
    store = ExperimentStore(tmp_path / "stale.db", create=True)
    store.append(manifest, idempotency_key="manifest")
    store.append(candidate, idempotency_key="candidate")
    assert module.baseline_diagnostics(store, manifest, observation,
        now=observation.recorded_at+timedelta(minutes=31)) == ()


def test_protected_archive_is_rejected_before_market_file_read(tmp_path, monkeypatch):
    from quantbot.learner.experiment_observation import build_observation_batch
    from quantbot.learner.forward_source import Snapshot

    module = import_module("quantbot.learner.exploration_launch")
    frozen, capture, _ = example()
    observation = build_observation_batch(frozen, capture, now=capture.received_at)
    source = SimpleNamespace(root=tmp_path, operations_database=tmp_path / "ops.db",
                             declaration_database=tmp_path / "declarations.db")
    monkeypatch.setattr(module, "_guard", lambda *args: False)
    monkeypatch.setattr(module, "Database", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(Snapshot, "model_validate_json", lambda *args, **kwargs:
                        pytest.fail("protected market document must not be parsed"))
    with pytest.raises(ValueError, match="PROTECTED_OBSERVATION_WINDOW"):
        module.archived_capture(source, frozen, observation)
