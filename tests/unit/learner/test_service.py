from datetime import UTC, datetime, timedelta

import pytest

from quantbot.learner import service
from quantbot.learner.data import build_dataset
from quantbot.learner.models import EvaluationClass
from quantbot.learner.repository import LearnerRepository
from quantbot.research.registry import DataRole
from quantbot.storage import Database
from tests.unit.learner.test_data import inputs_for, session_sequence, window


def dataset_fixture():
    sessions = session_sequence(120)
    return build_dataset(
        inputs_for(sessions, through=99),
        sessions,
        symbols=("AAPL", "MSFT"),
        universe_id="fixture",
        source_id="fixture-v1",
        train_window=window(DataRole.DISCOVERY, sessions, 60, 79),
        validation_window=window(DataRole.VALIDATION, sessions, 80, 99),
        holdout_window=window(DataRole.PROTECTED_EVALUATION, sessions, 100, 119),
    )


def test_train_replay_resolve_and_retrain_preserve_full_lineage(tmp_path):
    database = Database.create(tmp_path / "learner.db")
    dataset = dataset_fixture()
    service.register_dataset(database, dataset)
    first = service.train(database, dataset.identity)
    second = service.train(database, dataset.identity, parent_model_id=first.model_id)
    assert second.model_id != first.model_id
    assert second.artifact == first.artifact
    assert second.version == 2
    assert second.parent_model_id == first.model_id
    predictions = service.replay(database, first.model_id)
    assert predictions
    assert all(p.evaluation_class is EvaluationClass.HISTORICAL_REPLAY for p in predictions)
    with database.transaction() as session:
        repository = LearnerRepository(session)
        assert repository.get_model(first.model_id) == first
        assert all(repository.get_outcome(p.prediction_id) is None for p in predictions)
    with pytest.raises(ValueError, match="no resolved"):
        service.evaluate(database, first.model_id, EvaluationClass.HISTORICAL_REPLAY)
    sessions = session_sequence(120)
    resolved = service.resolve(
        database,
        first.model_id,
        inputs_for(sessions, through=99),
        source_id="fixture-v1",
    )
    assert len(resolved) == len(predictions)
    evaluation = service.evaluate(database, first.model_id, EvaluationClass.HISTORICAL_REPLAY)
    assert set(evaluation.prediction_ids) == {p.prediction_id for p in predictions}
    assert service.status(database)["positive_expectancy_justified"] is False
    database.close()


def test_public_predict_cannot_mint_backdated_shadow_predictions(tmp_path):
    database = Database.create(tmp_path / "learner.db")
    dataset = dataset_fixture()
    service.register_dataset(database, dataset)
    model = service.train(database, dataset.identity)
    features = tuple(r.features for r in dataset.rows if r.role is DataRole.VALIDATION)
    with pytest.raises(ValueError, match="target.*past|before.*target|unseen|validation"):
        service.issue_predictions(database, model.model_id, features[:2])
    database.close()


def test_shadow_prediction_commits_before_delayed_resolution(tmp_path, monkeypatch):
    database = Database.create(tmp_path / "learner.db")
    dataset = dataset_fixture()
    service.register_dataset(database, dataset)
    model = service.train(database, dataset.identity)
    as_of = datetime.now(UTC)
    features = tuple(
        row.features.model_copy(
            update={
                "as_of": as_of,
                "target_at": as_of + timedelta(days=7),
                "input_start_at": as_of - timedelta(days=70),
            }
        )
        for row in dataset.rows[:2]
    )
    monkeypatch.setattr(service, "_utcnow", lambda: as_of)
    predictions = service.issue_predictions(database, model.model_id, features)
    assert all(p.evaluation_class is EvaluationClass.PROSPECTIVE_LOCAL for p in predictions)
    with database.transaction() as session:
        stored = LearnerRepository(session).get_prediction(predictions[0].prediction_id)
        assert stored == predictions[0]
    assert service.resolve(database, model.model_id, (), source_id="fixture-v1") == ()
    database.close()


def test_comparison_never_silently_uses_training_metrics(tmp_path):
    database = Database.create(tmp_path / "learner.db")
    dataset = dataset_fixture()
    service.register_dataset(database, dataset)
    first = service.train(database, dataset.identity, family="mean")
    second = service.train(database, dataset.identity, parent_model_id=first.model_id)
    with pytest.raises(ValueError, match="resolved.*common"):
        service.compare(database, first.model_id, second.model_id)
    database.close()


def test_late_issuance_cannot_observe_part_of_the_target_and_claim_prediction(tmp_path):
    database = Database.create(tmp_path / "learner.db")
    dataset = dataset_fixture()
    service.register_dataset(database, dataset)
    model = service.train(database, dataset.identity)
    cutoff = datetime.now(UTC) - timedelta(days=1)
    features = tuple(
        row.features.model_copy(
            update={
                "as_of": cutoff,
                "target_at": cutoff + timedelta(days=7),
                "input_start_at": cutoff - timedelta(days=70),
            }
        )
        for row in dataset.rows[:2]
    )
    with pytest.raises(ValueError, match="issuance deadline"):
        service.issue_predictions(database, model.model_id, features)
    database.close()


def test_selected_subset_cannot_hide_missing_universe_members(tmp_path):
    database = Database.create(tmp_path / "learner.db")
    dataset = dataset_fixture()
    service.register_dataset(database, dataset)
    model = service.train(database, dataset.identity)
    with pytest.raises(ValueError, match="complete declared universe"):
        service.issue_predictions(database, model.model_id, (dataset.rows[0].features,))
    database.close()


def test_new_vendor_name_cannot_recycle_a_declared_holdout(tmp_path):
    database = Database.create(tmp_path / "learner.db")
    original = dataset_fixture()
    service.register_dataset(database, original)
    sessions = session_sequence(160)
    windows = tuple(
        window(role, sessions, start, end).model_copy(
            update={
                "dataset": "alpaca-us-equities-daily",
            }
        )
        for role, start, end in (
            (DataRole.DISCOVERY, 90, 119),
            (DataRole.VALIDATION, 120, 139),
            (DataRole.PROTECTED_EVALUATION, 140, 159),
        )
    )
    recycled = build_dataset(
        inputs_for(sessions, through=139),
        sessions,
        symbols=("AAPL", "MSFT"),
        universe_id="fixture",
        source_id="renamed-vendor",
        train_window=windows[0],
        validation_window=windows[1],
        holdout_window=windows[2],
    )
    with pytest.raises(ValueError, match="protected learner holdout"):
        service.register_dataset(database, recycled)
    database.close()
