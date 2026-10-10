"""Synthetic fixtures exercise read-only, after-fee exploration reporting."""
from datetime import timedelta
from decimal import Decimal
from importlib import import_module

import pytest

from quantbot.learner.exploration_feedback import journal_exploration_feedback
from tests.unit.learner.test_exploration_feedback import journal


def scorecard(store, policy, now):
    module = import_module("quantbot.console.experiments")
    reader = getattr(module, "exploration_scorecard", None)
    assert callable(reader), "Verified after-fee scorecard is not implemented"
    return reader(store, policy, now=now)


def test_no_feedback_is_unknown_not_zero_profit(tmp_path):
    store, policy, _, now = journal(tmp_path)
    before = store.path.read_bytes()
    result = scorecard(store, policy, now)
    assert result.verified_episodes == result.recorded_feedback == 0
    assert result.net_pnl is result.expectancy_usd is result.profit_factor is None
    assert result.improvement_established is False
    assert result.learning_effect == "ROUND_ROBIN_SELECTION_COUNT_ONLY"
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("fee,net,outcome", [(".20", "1.70", "WIN"),
                                             ("3", "-1.10", "LOSS"),
                                             ("1.90", "0", "FLAT")])
def test_fee_adjusted_outcomes_and_expectancy_without_double_costs(tmp_path, fee, net, outcome):
    store, policy, _, now = journal(tmp_path, exit_fee=Decimal(fee))
    journal_exploration_feedback(store, policy, now=now)
    before = store.path.read_bytes()
    result = scorecard(store, policy, now)
    assert result.verified_episodes == result.recorded_feedback == 1
    assert result.net_pnl == result.expectancy_usd == Decimal(net)
    assert result.verified_fees == Decimal(fee) + Decimal(".10")
    assert (result.profitable_episodes, result.losing_episodes, result.flat_episodes) == (
        int(outcome == "WIN"), int(outcome == "LOSS"), int(outcome == "FLAT"))
    assert result.profitable_episode_percent == (Decimal(100) if outcome == "WIN" else Decimal(0))
    assert result.synthetic is True
    assert result.evidence_class == "SYNTHETIC_TEST_ONLY"
    assert result.improvement_established is False
    assert store.path.read_bytes() == before


def test_future_feedback_does_not_enter_as_of_scorecard(tmp_path):
    store, policy, _, now = journal(tmp_path)
    journal_exploration_feedback(store, policy, now=now)
    result = scorecard(store, policy, now - timedelta(seconds=1))
    assert result.recorded_feedback == result.verified_episodes == 0
    assert result.net_pnl is None


def test_later_evidence_invalidates_feedback_without_erasing_receipt(tmp_path):
    from quantbot.learner.experiment_lots import LotEvent, record_lot_event

    store, policy, _, now = journal(tmp_path)
    journal_exploration_feedback(store, policy, now=now)
    fill = next(event.decode(LotEvent) for event in store.read_as_of(policy.experiment_id, now)
                if event.kind == "LotEvent" and event.decode(LotEvent).kind == "FILL")
    record_lot_event(store, fill.model_copy(update={
        "fill_id": "late-fill", "recorded_at": now + timedelta(seconds=1),
        "occurred_at": now + timedelta(seconds=1),
    }))
    result = scorecard(store, policy, now + timedelta(seconds=2))
    assert result.recorded_feedback == result.excluded_feedback == 1
    assert result.verified_episodes == 0 and result.net_pnl is None
    assert scorecard(store, policy, now).verified_episodes == 1


@pytest.mark.parametrize("changes", [{"synthetic": False},
                                    {"actual_net_return": Decimal("10")},
                                    {"observation_id": "f" * 64},
                                    {"recorded_at": None}])
def test_inconsistent_feedback_cannot_supply_economic_metrics(tmp_path, changes):
    from quantbot.learner.experiment_store import ExperimentStore

    store, policy, _, now = journal(tmp_path)
    feedback = journal_exploration_feedback(store, policy, now=now)[0]
    if changes == {"recorded_at": None}:
        changes = {"recorded_at": policy.recorded_at}
    isolated = ExperimentStore(tmp_path / "inconsistent.db", create=True)
    for index, event in enumerate(store.read_as_of(policy.experiment_id, now)):
        if event.kind != "ExplorationFeedback":
            record_type = import_module("quantbot.learner.exploration").ExplorationPolicy
            if event.kind == "ExperimentManifest":
                record_type = import_module(
                    "quantbot.learner.experiment_contract").ExperimentManifest
            elif event.kind == "ExplorationObservation":
                record_type = import_module("quantbot.learner.exploration").ExplorationObservation
            elif event.kind == "LotEvent":
                record_type = import_module("quantbot.learner.experiment_lots").LotEvent
            isolated.append(event.decode(record_type), idempotency_key=f"source-{index}")
    isolated.append(feedback.model_copy(update=changes), idempotency_key="inconsistent-feedback")
    result = scorecard(isolated, policy, now)
    assert result.recorded_feedback == result.excluded_feedback == 1
    assert result.verified_episodes == 0 and result.mean_net_return is None


def test_feedback_cannot_be_reassigned_to_another_registered_policy(tmp_path):
    store, policy, _, now = journal(tmp_path)
    feedback = journal_exploration_feedback(store, policy, now=now)[0]
    alternate = policy.model_copy(update={"code_hash": "e" * 64})
    store.append(alternate, idempotency_key="alternate-policy")
    store.append(feedback.model_copy(update={"policy_id": alternate.policy_id}),
                 idempotency_key="reassigned-feedback")
    result = scorecard(store, alternate, now)
    assert result.recorded_feedback == result.excluded_feedback == 1
    assert result.verified_episodes == 0 and result.net_pnl is None


def test_dashboard_html_and_json_share_verified_scorecard(tmp_path):
    from fastapi.testclient import TestClient

    from quantbot.console.app import create_app
    from quantbot.storage import Database

    store, policy, _, now = journal(tmp_path)
    journal_exploration_feedback(store, policy, now=now)
    before = store.path.read_bytes()
    with Database.create(tmp_path / "console.db") as database:
        client = TestClient(create_app(database, experiment_store=store.path))
        response = client.get("/experiments/" + policy.experiment_id)
        assert response.status_code == 200
        assert "Synthetic test evidence; not actual PAPER profit" in response.text
        assert "Predictor parameters are not fitted by this feedback" in response.text
        assert "$1.70" in response.text
        response = client.get("/api/experiments/" + policy.experiment_id)
        assert response.status_code == 200
        result = response.json()["detail"]["exploration_scorecards"][0]
        assert result["net_pnl"] == "1.70"
        assert result["verified_episodes"] == 1 and result["predictor_fit"] is False
    assert store.path.read_bytes() == before
