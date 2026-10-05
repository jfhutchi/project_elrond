"""Synthetic scheduled read-only integration, no provider or broker access."""

import json

import pytest

from quantbot.learner import tracking_launch as launch
from quantbot.learner.forward import _read, _save
from quantbot.research.manifest import content_id
from quantbot.storage import Database
from tests.unit.learner.test_tracking_feedback import NOW, model, source

_verify_source = launch.verify_source


@pytest.fixture(autouse=True)
def isolated_test_code(monkeypatch):
    monkeypatch.setattr(launch, "verify_source", lambda *_: None)


def configuration(tmp_path):
    source(tmp_path)
    op = tmp_path / "operational.db"
    Database.create(op).close()
    config = dict(
        root=str(tmp_path / "source"),
        frozen=str(tmp_path / "frozen"),
        operations_database=str(op),
        declaration_database=str(tmp_path / "declarations.db"),
        clock_receipt=str(tmp_path / "clock.json"),
        code_commit="a" * 40,
        spec_hash="a" * 64,
        frozen_data_identity="b" * 64,
        mean_model="a" * 64,
        ridge_model="b" * 64,
        mean_trial="intraday:dia-five-minute-" + "a" * 64,
        ridge_trial="intraday:dia-five-minute-" + "b" * 64,
    )
    _save(tmp_path / "producer.json", config)
    candidates = [model(NOW), model(NOW)]
    candidates[1]["family"] = "quadratic_ridge"
    candidates[1]["artifact"].update(
        family="quadratic_ridge",
        means=[0] * 20,
        scales=[1] * 20,
        coefficients=[0] * 20,
        algorithm_version="quadratic-ridge-squares-cholesky-v1",
    )
    paths = (tmp_path / "ridge.json", tmp_path / "quadratic.json")
    for path, candidate in zip(paths, candidates, strict=True):
        _save(path, candidate)
    (tmp_path / "clock.json").write_text(
        json.dumps(dict(measured_at=NOW.isoformat(), clock_status="HEALTHY"))
    )
    return launch.TrackingConfig(
        mode="PAPER",
        root=tmp_path / "tracking",
        source_config=tmp_path / "producer.json",
        source_config_hash=content_id(config),
        source_code_commit="a" * 40,
        strategy_id="selected",
        shadow_models=paths,
        shadow_hashes=tuple(content_id(c) for c in candidates),
        code_commit="c" * 40,
        source_tree_hash="d" * 64,
    ), op


def test_scheduled_tracking_collects_all_market_labels_without_operational_write(
    tmp_path, monkeypatch
):
    config, op = configuration(tmp_path)
    before = op.read_bytes()
    monkeypatch.setattr(launch, "_guard", lambda *_: True)
    receipt = launch.run_once(config, now=NOW)
    assert receipt["new_market_training_labels"] == 1
    assert receipt["execution_episodes"] == 0
    assert receipt["orders"] == receipt["fits"] == receipt["promotions"] == 0
    assert receipt["active_model_changed"] is False
    assert op.read_bytes() == before
    assert launch.run_once(config, now=NOW)["new_market_training_labels"] == 0
    assert len(list((config.root / "runs").glob("*.json"))) == 2


@pytest.mark.parametrize("change", ["sourcehash", "shadowhash", "overlap", "frozen-overlap"])
def test_binding_changes_fail_before_tracking_writes(tmp_path, change):
    config, _ = configuration(tmp_path)
    if change == "sourcehash":
        config = config.model_copy(update={"source_config_hash": "f" * 64})
    if change == "shadowhash":
        config = config.model_copy(update={"shadow_hashes": ("f" * 64, "e" * 64)})
    if change == "overlap":
        config = config.model_copy(update={"root": tmp_path / "source" / "nested"})
    if change == "frozen-overlap":
        config = config.model_copy(update={"root": tmp_path / "frozen" / "nested"})
    with pytest.raises(ValueError):
        launch.run_once(config, now=NOW)
    assert not config.root.exists()


def test_missing_clock_blocks_new_predictions_but_preserves_past_outcome_accounting(
    tmp_path, monkeypatch
):
    config, _ = configuration(tmp_path)
    (tmp_path / "clock.json").unlink()
    monkeypatch.setattr(launch, "_guard", lambda *_: True)
    receipt = launch.run_once(config, now=NOW)
    assert receipt["status"] == "CLOCK_BLOCKED_NEW_SHADOW_FORECASTS"
    assert receipt["new_market_training_labels"] == 1
    assert receipt["raw_shadow"] == dict(predictions=0, outcomes=0)
    assert _read(config.root / "deployment.json")["mode"] == "PAPER"


def test_executable_pin_rejects_changed_source_or_revision(tmp_path):
    config, _ = configuration(tmp_path)
    package = tmp_path / "release" / "src" / "quantbot"
    package.mkdir(parents=True)
    (package / "probe.py").write_text("x=1")
    (package.parent.parent / "REVISION").write_text(config.code_commit)
    config = config.model_copy(update={"source_tree_hash": launch.source_tree_hash(package)})
    _verify_source(config, package)
    (package / "probe.py").write_text("x=2")
    with pytest.raises(ValueError):
        _verify_source(config, package)
