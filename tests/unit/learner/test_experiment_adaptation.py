"""Fixed forward folds update research without transferring order authority."""

from datetime import timedelta
from importlib import import_module

import pytest

from quantbot.learner.experiment_coordinator import ResolvedMarketLabel, feature_key
from quantbot.learner.experiment_store import ExperimentStore
from tests.fixtures.adaptive_paper import manifest
from tests.unit.learner.test_experiment_research import (
    Charger,
    Guard,
    fake_artifact,
    inputs,
    request,
)


def module():
    return import_module("quantbot.learner.experiment_adaptation")


@pytest.fixture
def context(tmp_path):
    research = import_module("quantbot.learner.experiment_research")
    store = ExperimentStore(tmp_path / "workflow.db", create=True)
    frozen = manifest()
    store.append(frozen, idempotency_key="manifest")
    charger = Charger()
    parent = research.run_registered_fit(
        store,
        frozen,
        request(),
        guard=Guard(),
        charger=charger,
        now=frozen.registered_at,
        synthetic_engine=fake_artifact,
    )
    return store, charger, parent


def forward_labels(store, count):
    originals = inputs()[1][:6]
    result = []
    day = manifest().prospective_start_at + timedelta(hours=1)
    for _ in range(count):
        while day.weekday() > 4:
            day += timedelta(days=1)
        for original in originals:
            delta = day - original.features.feature_at
            features = original.features.model_copy(
                update={
                    field: getattr(original.features, field) + delta
                    for field in ("feature_at", "decision_at", "target_at", "receipt_at")
                }
            )
            label = original.model_copy(
                update={
                    "features": features,
                    "available_at": original.available_at + delta,
                    "raw_return": 0.04,
                    "benchmark_return": 0.01,
                    "excess_return": 0.03,
                }
            )
            record = ResolvedMarketLabel(
                experiment_id=manifest().experiment_id,
                recorded_at=label.available_at,
                label=label,
                synthetic=True,
            )
            store.append(record, idempotency_key="label:" + feature_key(features))
            result.append(label)
        day += timedelta(days=1)
    return tuple(result)


def test_five_new_sessions_do_not_trigger_identical_refit_or_reuse_validation(context):
    store, charger, _ = context
    labels = forward_labels(store, 5)
    result = module().schedule_next_successor(
        store, manifest(), now=labels[-1].available_at, guard=Guard()
    )
    assert result.status == "NORMAL_WAIT"
    assert result.reason == "FRESH_TRAINING_AND_UNSEEN_VALIDATION_REQUIRED"
    assert len(charger.calls) == 1
    assert not result.grants_activation


def test_ten_new_sessions_queue_updated_training_once_without_promotion(context):
    research = import_module("quantbot.learner.experiment_research")
    store, charger, parent = context
    labels = forward_labels(store, 10)
    now = labels[-1].available_at
    planned = module().schedule_next_successor(store, manifest(), now=now, guard=Guard())
    assert planned.status == "QUEUED"
    pending = research.research_eligibility(store, manifest(), now=now)
    assert pending.request_id == planned.request_id
    assert pending.parent_model_id == parent.candidate_id
    assert pending.training_sessions == 25
    assert pending.validation_sessions == 5
    assert not any(row in pending.training for row in inputs()[1])
    again = module().schedule_next_successor(store, manifest(), now=now, guard=Guard())
    assert again.request_id == planned.request_id
    assert len(charger.calls) == 1
    child = research.run_registered_fit(
        store,
        manifest(),
        pending,
        now=now,
        guard=Guard(),
        charger=charger,
        synthetic_engine=fake_artifact,
    )
    assert child.candidate_id != parent.candidate_id
    assert len(charger.calls) == 2
    assert store.state(manifest().experiment_id).activation_id is None


def test_duplicate_parent_receipt_cannot_choose_a_successor(context):
    store, charger, fit = context
    store.append(fit, idempotency_key="duplicate-parent-receipt")
    labels = forward_labels(store, 10)
    with pytest.raises(ValueError, match="AUTHORITATIVE_SUCCESSOR_PARENT_REQUIRED"):
        module().schedule_next_successor(
            store, manifest(), now=labels[-1].available_at, guard=Guard()
        )
    assert len(charger.calls) == 1
