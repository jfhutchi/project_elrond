"""Synthetic end-to-end stored-bar replay through the ordinary CLI."""

import io
import json

import pytest

from quantbot.cli import main
from quantbot.learner.store import StoreReplaySpec
from quantbot.research.manifest import content_id
from tests.unit.learner.test_store import stored


def test_stored_replay_cli_preserves_model_outcome_and_report_lineage(tmp_path):
    database, calendar = stored(tmp_path)
    database.close()
    dates = [s.session_date for s in calendar.sessions]
    spec = StoreReplaySpec(sessions=calendar, symbols=("AAPL", "MSFT"),
                          train_start=dates[60], train_end=dates[70],
                          validation_start=dates[71], validation_end=dates[84],
                          holdout_start=dates[85], holdout_end=dates[94])
    spec_file = tmp_path / "spec.json"
    calendar_file = tmp_path / "calendar.json"
    spec_file.write_text(spec.model_dump_json(), encoding="utf-8")
    calendar_file.write_text(calendar.model_dump_json(), encoding="utf-8")

    def command(*args):
        output = io.StringIO()
        code = main(["learner", "--database", str(tmp_path / "research.db"), *args], output=output)
        result = json.loads(output.getvalue())
        assert code == 0, result
        return result

    dataset = command("dataset-from-store", "--spec", str(spec_file))
    assert dataset["source_evidence"] == "LOCAL_DECLARED_HISTORICAL_STORE_REPLAY"
    model = command("train", dataset["dataset_id"], "--family", "ridge")["model"]
    command("predict", model["model_id"], "--replay")
    assert command("resolve-from-store", model["model_id"], "--calendar", str(calendar_file))[
        "resolved"] > 0
    assert command("resolve-from-store", model["model_id"], "--calendar", str(calendar_file))[
        "resolved"] == 0
    command("evaluate", model["model_id"], "--evaluation-class", "HISTORICAL_REPLAY")
    report_file = tmp_path / "economics.json"
    command("economics", model["model_id"], "--calendar", str(calendar_file),
            "--output", str(report_file))
    payload = json.loads(report_file.read_text(encoding="utf-8"))
    report = payload["report"]
    assert payload["report_hash"] == content_id(report)
    assert report["lineage"]["model_id"] == model["model_id"]
    assert report["positive_expectancy_justified"] is False
    assert report["promotion"] == "NONE"
    assert all(item["bar"]["timestamp"] < calendar.sessions[85].close_at.isoformat()
               for item in report["execution_inputs"])

    # An interrupted replay can leave a valid first cohort. It is not complete validation.
    from sqlalchemy import delete, select, update

    from quantbot.learner.economics import evaluate_store
    from quantbot.storage import Database
    from quantbot.storage.database import encode_utc
    from quantbot.storage.schema import (
        bars,
        learner_evaluations,
        learner_outcomes,
        learner_predictions,
    )

    database = Database(tmp_path / "research.db")
    with database.transaction() as session:
        bar_id, original_open = session.execute(select(bars.c.bar_id, bars.c.open).where(
            bars.c.symbol == "AAPL",
            bars.c.timestamp == encode_utc(calendar.sessions[72].close_at),
        )).one()
        session.execute(update(bars).where(bars.c.bar_id == bar_id).values(open=bars.c.close))
    with pytest.raises(ValueError, match="source snapshot"):
        evaluate_store(database, model["model_id"], calendar)
    from quantbot.learner.store import resolve_from_store

    with pytest.raises(ValueError, match="source snapshot"):
        resolve_from_store(database, model["model_id"], calendar)
    with database.transaction() as session:
        session.execute(update(bars).where(bars.c.bar_id == bar_id).values(open=original_open))
    with database.transaction() as session:
        session.execute(delete(learner_evaluations))
        session.execute(delete(learner_outcomes).where(
            learner_outcomes.c.prediction_id.in_(report["prediction_ids"][-2:])))
        session.execute(delete(learner_predictions).where(
            learner_predictions.c.prediction_id.in_(report["prediction_ids"][-2:])))
    with pytest.raises(ValueError, match="complete validation"):
        evaluate_store(database, model["model_id"], calendar)
    database.close()


def test_fixed_runner_counts_three_fits_and_refuses_restart_over_evidence(tmp_path):
    from scripts.learner_market_replay import run_replay
    from sqlalchemy import func, select

    from quantbot.storage import Database
    from quantbot.storage.schema import search_runs

    database, calendar = stored(tmp_path)
    database.close()
    dates = [s.session_date for s in calendar.sessions]
    spec = StoreReplaySpec(sessions=calendar, symbols=("AAPL", "MSFT"),
                          train_start=dates[60], train_end=dates[70],
                          validation_start=dates[71], validation_end=dates[84],
                          holdout_start=dates[85], holdout_end=dates[94])
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(spec.model_dump_json(), encoding="utf-8")
    path = tmp_path / "research.db"
    report = run_replay(path, spec_file, tmp_path / "run")
    assert report["models"]["ridge"]["artifact_hash"] == report["models"][
        "ridge_successor"]["artifact_hash"]
    assert report["models"]["ridge"]["model_id"] != report["models"][
        "ridge_successor"]["model_id"]
    with pytest.raises(FileExistsError):
        run_replay(path, spec_file, tmp_path / "run")
    database = Database(path)
    with database.transaction() as session:
        assert session.scalar(select(func.sum(search_runs.c.candidates_evaluated))) == 3
    database.close()
