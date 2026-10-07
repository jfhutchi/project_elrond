"""Outcome feedback uses owned synthetic fill evidence, never market labels."""
from datetime import timedelta
from decimal import Decimal
from importlib import import_module

import pytest

from quantbot.learner.experiment_lots import LotEvent, record_lot_event
from quantbot.learner.experiment_observation import build_observation_batch
from quantbot.learner.experiment_store import ExperimentStore
from tests.fixtures.adaptive_paper import OPEN
from tests.unit.learner.test_experiment_lots import prepare
from tests.unit.learner.test_paper_exploration import example


def journal(tmp_path, *, exit_fee=Decimal(".20"), exit_quantity=Decimal(1)):
    module = import_module("quantbot.learner.exploration")
    frozen, source, policy = example()
    batch = build_observation_batch(frozen, source, now=source.received_at)
    observation = module.exploration_observations(
        frozen, policy, batch, source=source, baseline_decisions=(),
        holdout_guard=lambda start, end: True,
    )[0]
    original = prepare(tmp_path / "original", exit_fee=exit_fee, exit_quantity=exit_quantity)
    store = ExperimentStore(tmp_path / "feedback.db", create=True)
    store.append(frozen, idempotency_key="manifest")
    store.append(policy, idempotency_key="policy:" + policy.policy_id)
    store.append(observation, idempotency_key="observation:" + observation.prediction_id)
    for event in original.read_as_of(frozen.experiment_id, OPEN + timedelta(seconds=30)):
        if event.kind != "LotEvent":
            continue
        row = event.decode(LotEvent)
        row = row.model_copy(update={
            "decision_id": observation.prediction_id, "symbol": observation.features.symbol,
            "occurred_at": row.occurred_at + timedelta(minutes=35, seconds=2),
            "recorded_at": row.recorded_at + timedelta(minutes=35, seconds=2),
        })
        record_lot_event(store, row)
    return store, policy, observation, OPEN + timedelta(minutes=35, seconds=40)


def test_completed_protected_owned_episode_yields_immutable_feedback(tmp_path):
    module = import_module("quantbot.learner.exploration_feedback")
    store, policy, observation, now = journal(tmp_path)
    feedback = module.journal_exploration_feedback(store, policy, now=now)
    assert len(feedback) == 1
    row = feedback[0]
    assert row.observation_id == observation.prediction_id
    assert row.result == "WIN" and row.net_pnl == Decimal("1.70")
    assert row.actual_fees == Decimal(".30")
    assert row.actual_net_return == Decimal(".017")
    assert row.predictor_fit is False and row.grants_execution is False
    assert row.synthetic is True
    assert module.journal_exploration_feedback(store, policy, now=now+timedelta(minutes=1)) == ()


@pytest.mark.parametrize("failure", ["fees", "residual"])
def test_unknown_or_incomplete_economics_cannot_be_learning_outcomes(tmp_path, failure):
    module = import_module("quantbot.learner.exploration_feedback")
    store, policy, _, now = journal(
        tmp_path, exit_fee=None if failure == "fees" else Decimal(".20"),
        exit_quantity=Decimal(".4") if failure == "residual" else Decimal(1),
    )
    assert module.journal_exploration_feedback(store, policy, now=now) == ()
    assert all(row.kind != "ExplorationFeedback"
               for row in store.read_as_of(policy.experiment_id, now))


@pytest.mark.parametrize("failure", ["fees", "residual", "repeated-cohort", "none"])
def test_prior_episode_must_complete_before_a_new_fresh_cohort(tmp_path, failure):
    module = import_module("quantbot.learner.exploration_execution")
    store, policy, observation, now = journal(
        tmp_path, exit_fee=None if failure == "fees" else Decimal(".20"),
        exit_quantity=Decimal(".4") if failure == "residual" else Decimal(1),
    )
    at = observation.features.feature_at
    if failure != "repeated-cohort":
        at += timedelta(minutes=5)
    objections = module.prior_episode_objections(store, policy, cohort_at=at, now=now)
    assert bool(objections) == (failure != "none")
