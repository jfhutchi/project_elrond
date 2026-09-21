"""Offline lifecycle tests; no broker, provider or runtime environment access."""

from datetime import timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import func, select

from quantbot.learner import forward
from quantbot.learner.data import DailyInput
from quantbot.learner.forward_source import Snapshot
from quantbot.storage import Database
from quantbot.storage.schema import search_runs
from tests.unit.learner.test_intraday import OPEN, bars, calendar


def snapshot(minute):
    received = OPEN + timedelta(minutes=minute, seconds=1)
    return cast(
        Snapshot,
        SimpleNamespace(
            received_at=received,
            identity="a" * 64,
            sessions=calendar(),
            inputs=tuple(DailyInput(bar=b, available_at=received) for b in bars(minute)),
        ),
    )


def run(root, db, minute, monkeypatch, source=None):
    data = source or snapshot(minute)
    monkeypatch.setattr(forward, "_now", lambda: data.received_at)
    return forward.process_snapshot(
        root, db, data, code_commit="b" * 40, config=forward.ForwardConfig(symbols=("AAPL",))
    )


def test_observations_resolve_once_without_inventing_model_forecasts(tmp_path, monkeypatch):
    with Database.create(tmp_path / "learner.db") as db:
        first = run(tmp_path, db, 31, monkeypatch)
        again = run(tmp_path, db, 31, monkeypatch)
        later = run(tmp_path, db, 61, monkeypatch)
        assert first["observations_this_run"] == 1
        assert again["observations_this_run"] == 0
        assert later["outcomes_this_run"] == 1
        assert later["predictions_this_run"] == 0
        assert later["training_status"] == "TRAINING_DATA_WARMUP"
        assert run(tmp_path, db, 61, monkeypatch)["outcomes_this_run"] == 0


def test_incomplete_data_does_not_consume_observation_slot(tmp_path, monkeypatch):
    with Database.create(tmp_path / "learner.db") as db:
        broken = snapshot(31)
        broken.inputs = broken.inputs[:-1]
        result = run(tmp_path, db, 31, monkeypatch, broken)
        assert "INCOMPLETE_OR_STALE_MINUTE_DATA" in result["blockers"]
        assert result["observation_cohorts"] == 0
        assert run(tmp_path, db, 31, monkeypatch)["observations_this_run"] == 1


def test_protected_capture_is_never_analyzed(tmp_path, monkeypatch):
    def protected(*args, **kwargs):
        raise ValueError("protected")

    monkeypatch.setattr(forward.service, "guard_range", protected)
    monkeypatch.setattr(forward, "intraday_features", lambda *a, **k: pytest.fail("protected read"))
    with Database.create(tmp_path / "learner.db") as db:
        result = run(tmp_path, db, 31, monkeypatch)
        assert result["observations_this_run"] == 0
        assert "PROTECTED_FEATURE_WINDOW" in result["blockers"]


def test_failed_fit_stays_charged_and_cannot_automatically_repeat(tmp_path, monkeypatch):
    with Database.create(tmp_path / "learner.db") as db:
        run(tmp_path, db, 31, monkeypatch)
        run(tmp_path, db, 61, monkeypatch)
        root = tmp_path / "intraday"
        original_path = next((root / "outcomes").glob("*/*.json"))
        original = forward._read(original_path)["labels"][0]
        from quantbot.learner.intraday import IntradayLabel

        label = IntradayLabel.model_validate(original)
        for day in range(1, 25):
            delta = timedelta(days=day)
            features = label.features.model_copy(
                update={
                    name: getattr(label.features, name) + delta
                    for name in ("decision_at", "feature_at", "target_at", "receipt_at")
                }
            )
            shifted = label.model_copy(
                update=dict(features=features, available_at=label.available_at + delta)
            )
            forward._save(
                root / "outcomes" / features.decision_at.date().isoformat() / "1400.json",
                dict(labels=[shifted.model_dump(mode="json")]),
            )
        monkeypatch.setattr(forward, "_now", lambda: OPEN + timedelta(days=26))

        def failed_fit(*args, **kwargs):
            raise RuntimeError("synthetic fit interruption")

        monkeypatch.setattr(forward, "fit_intraday_model", failed_fit)
        with pytest.raises(RuntimeError):
            forward._train_challengers(root, db, forward.ForwardConfig(symbols=("AAPL",)), "b" * 40)
        assert forward._train_challengers(
            root, db, forward.ForwardConfig(symbols=("AAPL",)), "b" * 40
        ) == (
            0,
            "TRAINING_ATTEMPT_REQUIRES_REVIEW",
        )
        # New outcomes must not restart a failed fit as a supposedly different experiment.
        extra = root / "outcomes" / (OPEN + timedelta(days=25)).date().isoformat() / "1400.json"
        forward._save(extra, dict(labels=[shifted.model_dump(mode="json")]))
        assert forward._train_challengers(
            root, db, forward.ForwardConfig(symbols=("AAPL",)), "b" * 40
        ) == (
            0,
            "TRAINING_ATTEMPT_REQUIRES_REVIEW",
        )
        with db.transaction() as session:
            assert session.scalar(select(func.count()).select_from(search_runs)) == 2


def test_real_model_forecast_is_scored_at_next_minute_open_after_recording(tmp_path, monkeypatch):
    from quantbot.learner.intraday import build_historical_labels, fit_intraday_model

    labels = build_historical_labels(
        bars(180),
        calendar(),
        decision_times=[OPEN + timedelta(minutes=m) for m in (31, 60, 120)],
        symbols=("AAPL",),
        source_snapshot_hash="a" * 64,
        observed_at=OPEN + timedelta(minutes=180),
    )
    model = fit_intraday_model(
        labels,
        validation_start=OPEN + timedelta(minutes=100),
        created_at=OPEN + timedelta(minutes=181),
        code_commit="b" * 40,
        family="mean",
    )
    root = tmp_path / "intraday"
    forward._save(root / "models" / (model.model_id + ".json"), model.model_dump(mode="json"))
    forward._save(
        root / "fits" / ("c" * 64 + ".json"), dict(model_ids=[model.model_id], search_ids=[])
    )
    with Database.create(tmp_path / "learner.db") as db:
        assert run(tmp_path, db, 200, monkeypatch)["predictions_this_run"] == 1
        assert run(tmp_path, db, 200, monkeypatch)["predictions_this_run"] == 0
        result = run(tmp_path, db, 230, monkeypatch)
        assert result["performance"][0]["matched_periods"] == 1
        score_path = next((root / "scores").glob("*/*.json"))
        score = forward._read(score_path)
        assert score["entry_at"] == (OPEN + timedelta(minutes=201)).isoformat()
        assert score["exit_at"] == (OPEN + timedelta(minutes=230)).isoformat()
        # Synthetic AAPL close at minute229, executable next-minute open201, 5bps each side.
        assert score["net_return"] == pytest.approx(329 * 0.9995 / (301 * 1.0005) - 1)
        assert score["paper_fills"] is False


def test_roster_or_policy_change_cannot_reuse_experiment(tmp_path, monkeypatch):
    with Database.create(tmp_path / "learner.db") as db:
        run(tmp_path, db, 31, monkeypatch)
        with pytest.raises(ValueError, match="immutable forward record conflict"):
            forward.process_snapshot(
                tmp_path,
                db,
                snapshot(41),
                code_commit="b" * 40,
                config=forward.ForwardConfig(symbols=("QQQ",)),
            )


def test_current_registry_is_consulted_without_schema_migration(tmp_path, monkeypatch):
    import sqlite3

    registry_path = (tmp_path / "operational.db").resolve()
    with Database.create(registry_path):
        pass
    with sqlite3.connect(registry_path) as connection:
        connection.execute("UPDATE schema_version SET version=28")
    calls = []

    def protected(self, dataset, start, end, **kwargs):
        calls.append((dataset, start, end))
        return ["protected"]

    monkeypatch.setattr(forward.HypothesisRegistry, "blocking_protected_windows", protected)
    token = forward._REGISTRY.set(registry_path)
    try:
        with Database.create(tmp_path / "learner.db") as db:
            assert not forward._guard(db, OPEN, OPEN + timedelta(minutes=30))
        assert calls[0][0] == "us-equities-daily"
        with sqlite3.connect(registry_path) as connection:
            assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 28
    finally:
        forward._REGISTRY.reset(token)


@pytest.mark.asyncio
async def test_long_outage_fetches_oldest_pending_day_without_gap_predictions(
    tmp_path, monkeypatch
):
    from quantbot.config import Settings

    with Database.create(tmp_path / "learner.db") as db:
        run(tmp_path, db, 31, monkeypatch)
        now = OPEN + timedelta(days=10)
        monkeypatch.setattr(forward, "_now", lambda: now)
        requests, forecast_permissions = [], []

        async def collect(directory, symbols, start, end, settings, **kwargs):
            requests.append((start, end))
            return snapshot(31)

        def process(*args, issue_forecasts=True, **kwargs):
            forecast_permissions.append(issue_forecasts)
            return dict(outcomes_this_run=1)

        monkeypatch.setattr(forward, "collect_snapshot", collect)
        monkeypatch.setattr(forward, "process_snapshot", process)
        token = forward._REGISTRY.set(db.path.resolve())
        try:
            result = await forward._collect_and_process(
                tmp_path, db, Settings(), "b" * 40, forward.ForwardConfig(symbols=("AAPL",))
            )
        finally:
            forward._REGISTRY.reset(token)
        assert requests[0] == (OPEN.date(), OPEN.date())
        assert requests[1] == ((now - timedelta(days=7)).date(), now.date())
        assert forecast_permissions == [False, True]
        assert result["outcomes_this_run"] == 2
