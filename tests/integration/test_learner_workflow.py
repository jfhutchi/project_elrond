from __future__ import annotations

import json

import pytest
from scripts.learner_demo import run_demo

from quantbot.learner.cli import InputBundle
from quantbot.learner.models import EvaluationClass
from quantbot.learner.repository import LearnerRepository
from quantbot.storage import Database


def test_local_synthetic_demo_runs_the_durable_learning_workflow(tmp_path) -> None:
    output_dir = tmp_path / "learner-demo"

    report = run_demo(output_dir)

    assert report["evidence_class"] == "LOCAL_SYNTHETIC_SOFTWARE_DEMONSTRATION"
    assert report["positive_expectancy_justified"] is False
    assert report["forward_paper_evidence"] == "NOT_ESTABLISHED"
    assert report["protected_holdout_evidence"] == "NOT_EVALUATED"
    assert report["promotion"] == "NONE"
    data = report["data"]
    assert data["calendar_label"] == "SYNTHETIC_CALENDAR"
    assert data["session_count"] > 160
    assert data["last_input_session_index"] == 139
    assert data["holdout_session_indices"] == [140, 159]
    assert data["holdout_inputs_loaded"] == 0

    saved = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
    assert saved == report
    bundle = InputBundle.model_validate_json(
        (output_dir / "input_bundle.json").read_text(encoding="utf-8")
    )
    assert (
        max(item.bar.timestamp for item in bundle.inputs)
        == bundle.sessions.sessions[139].close_at
    )
    assert all(
        item.bar.timestamp < bundle.sessions.sessions[140].close_at for item in bundle.inputs
    )

    models = report["models"]
    assert set(models) == {"mean", "ridge", "quadratic_ridge", "ridge_v2"}
    baseline_check = report["validation_baseline_check"]
    assert baseline_check["mean_validation_mse"] == models["mean"]["validation_mse"]
    assert baseline_check["ridge_validation_mse"] == models["ridge"]["validation_mse"]
    assert baseline_check["ridge_minus_mean_mse"] == pytest.approx(
        models["ridge"]["validation_mse"] - models["mean"]["validation_mse"]
    )
    assert baseline_check["ridge_beats_mean"] is False
    assert baseline_check["outcome"] == "RIDGE_DID_NOT_BEAT_MEAN_BASELINE"
    assert models["ridge"]["validation_mse"] > models["mean"]["validation_mse"]
    assert models["ridge_v2"]["version"] == 2
    assert models["ridge_v2"]["parent_model_id"] == models["ridge"]["model_id"]
    assert report["comparison"]["matched_observations"] > 0
    assert report["comparison"]["positive_expectancy_justified"] is False

    database = Database(output_dir / "learner.db")
    try:
        with database.transaction() as session:
            repository = LearnerRepository(session)
            stored_models = repository.list_models()
            predictions = repository.list_predictions()
            assert len(stored_models) == 4
            assert predictions
            assert all(
                prediction.evaluation_class is EvaluationClass.HISTORICAL_REPLAY
                for prediction in predictions
            )
            assert all(
                repository.get_outcome(item.prediction_id) is not None
                for item in predictions
            )
    finally:
        database.close()


def test_local_synthetic_demo_refuses_an_existing_output_path(tmp_path) -> None:
    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    sentinel = output_dir / "keep.txt"
    sentinel.write_text("operator data", encoding="utf-8")

    with pytest.raises(FileExistsError, match="output directory already exists"):
        run_demo(output_dir)

    assert sentinel.read_text(encoding="utf-8") == "operator data"
