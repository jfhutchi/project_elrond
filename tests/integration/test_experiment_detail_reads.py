"""Experiment detail retains complete evidence without nested full journal queries."""
from datetime import timedelta
from importlib import import_module

from quantbot.console.evidence_readiness import read_evidence_readiness
from quantbot.console.experiments import read_experiment_panel
from quantbot.learner.experiment_contract import ExperimentManifest
from quantbot.learner.experiment_evaluation import evaluate_experiment
from quantbot.learner.exploration_feedback import journal_exploration_feedback
from quantbot.learner.exploration_scorecard import exploration_scorecard
from tests.unit.learner.test_exploration_feedback import journal


def test_detail_queries_each_complete_as_of_journal_once(tmp_path, monkeypatch):
    store, policy, _, now = journal(tmp_path)
    journal_exploration_feedback(store, policy, now=now)
    policies = [policy]
    for index in range(3):
        alternate = policy.model_copy(update={"code_hash": str(index) * 64})
        store.append(alternate, idempotency_key="policy-" + str(index))
        policies.append(alternate)
    events = store.read_as_of(policy.experiment_id, now)
    manifest = next(event.decode(ExperimentManifest) for event in events
                    if event.kind == "ExperimentManifest")
    expected_evaluation = evaluate_experiment(store, manifest, now=now)
    expected_scorecards = tuple(exploration_scorecard(store, row, now=now) for row in policies)
    expected_readiness = read_evidence_readiness(store, manifest, now=now)
    before = store.path.read_bytes()
    # Observe actual SQLite statements, including nested lot namespace discovery.
    module = import_module("quantbot.learner.experiment_store")
    connect = module.sqlite3.connect
    statements = []

    def traced_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(module.sqlite3, "connect", traced_connect)
    result = read_experiment_panel(store.path, experiment_id=policy.experiment_id, now=now)
    assert result.available and result.detail is not None
    assert result.detail.evaluation == expected_evaluation
    assert result.detail.exploration_scorecards == expected_scorecards
    assert result.detail.evidence_readiness == expected_readiness
    assert result.detail.exploration_scorecards[0].net_pnl == expected_scorecards[0].net_pnl
    assert sum(statement.startswith("SELECT * FROM events WHERE experiment_id=")
               for statement in statements) == 1
    assert store.path.read_bytes() == before


def test_separate_panel_requests_refresh_as_of_evidence(tmp_path):
    store, policy, _, now = journal(tmp_path)
    before = read_experiment_panel(store.path, experiment_id=policy.experiment_id, now=now)
    journal_exploration_feedback(store, policy, now=now)
    after = read_experiment_panel(store.path, experiment_id=policy.experiment_id, now=now)
    earlier = read_experiment_panel(store.path, experiment_id=policy.experiment_id,
                                    now=now - timedelta(seconds=1))
    assert before.detail is not None and after.detail is not None and earlier.detail is not None
    assert before.detail.exploration_scorecards[0].recorded_feedback == 0
    assert after.detail.exploration_scorecards[0].verified_episodes == 1
    assert earlier.detail.exploration_scorecards[0].recorded_feedback == 0


def test_detail_still_refuses_corrupted_unrelated_evidence(tmp_path):
    store, policy, _, now = journal(tmp_path)
    with store._transaction() as db:
        db.execute("UPDATE events SET digest=? WHERE kind='ExplorationObservation'", ("f" * 64,))
    result = read_experiment_panel(store.path, experiment_id=policy.experiment_id, now=now)
    assert result.available is False
    assert result.reason == "EXPERIMENT_STORE_UNAVAILABLE_OR_UNVERIFIED"


def test_detail_preserves_cross_experiment_lot_identity_checks(tmp_path):
    from quantbot.learner.experiment_lots import LotEvent

    store, policy, _, now = journal(tmp_path)
    events = store.read_as_of(policy.experiment_id, now)
    manifest = next(event.decode(ExperimentManifest) for event in events
                    if event.kind == "ExperimentManifest")
    other = manifest.model_copy(update={"experiment_id": "other-experiment"})
    store.append(other, idempotency_key="other-manifest")
    for index, event in enumerate(events):
        if event.kind == "LotEvent":
            store.append(event.decode(LotEvent).model_copy(update={
                "experiment_id": other.experiment_id,
            }), idempotency_key="other-lot-" + str(index))
    expected = evaluate_experiment(store, manifest, now=now)
    result = read_experiment_panel(store.path, experiment_id=policy.experiment_id, now=now)
    assert result.available and result.detail is not None
    assert len(result.experiments) == 2
    assert result.detail.evaluation == expected
    assert result.detail.evaluation.completed_owned_lots == 0
    assert result.detail.evaluation.incomplete_owned_lots == 1
