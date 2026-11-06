from datetime import timedelta

import pytest

from tests.unit.learner.test_context_admission import setup_plan


def test_teacher_plan_adoption_preserves_old_plan_and_budget(tmp_path):
    from quantbot.learner.context_admission import (
        ContextEvaluationPlan,
        adopt_context_plan,
        load_context_plan,
    )

    _, store, registry, old = setup_plan(tmp_path)
    new = ContextEvaluationPlan.model_validate(
        old.model_dump()
        | {
            "target": "MODEL_TEACHER_AGREEMENT_V1",
            "teacher_policy_id": "f" * 64,
        }
    )
    assert adopt_context_plan(
        store,
        new,
        supersedes_plan_id=old.plan_id,
        registry_path=registry,
        declaration_path=registry,
        now=old.recorded_at,
    )
    assert (
        load_context_plan(
            store, experiment_id=old.experiment_id, plan_id=new.plan_id, at=old.recorded_at
        )
        == new
    )
    with pytest.raises(ValueError, match="SUPERSEDED"):
        load_context_plan(
            store, experiment_id=old.experiment_id, plan_id=old.plan_id, at=old.recorded_at
        )
    with pytest.raises(ValueError, match="FUTURE"):
        adopt_context_plan(
            store,
            new,
            supersedes_plan_id=new.plan_id,
            registry_path=registry,
            declaration_path=registry,
            now=old.training_start + timedelta(seconds=1),
        )


def test_adoption_cannot_amend_windows_or_spent_fold(tmp_path):
    from quantbot.learner.context_admission import ContextEvaluationPlan, adopt_context_plan
    from quantbot.learner.context_integration import ContextFitReservation

    _, store, registry, old = setup_plan(tmp_path)
    payload = old.model_dump() | {
        "target": "MODEL_TEACHER_AGREEMENT_V1",
        "teacher_policy_id": "f" * 64,
    }
    amended = ContextEvaluationPlan.model_validate(
        payload | {"evaluation_end": old.evaluation_end + timedelta(days=1)}
    )
    with pytest.raises(ValueError, match="UNCHANGED_WINDOW"):
        adopt_context_plan(
            store,
            amended,
            supersedes_plan_id=old.plan_id,
            registry_path=registry,
            declaration_path=registry,
            now=old.recorded_at,
        )
    store.append(
        ContextFitReservation(
            experiment_id=old.experiment_id,
            recorded_at=old.recorded_at,
            request_id="a" * 64,
            trial_id="synthetic-reservation",
            manifest_hash=old.manifest_hash,
        ),
        idempotency_key="synthetic-reservation",
    )
    with pytest.raises(ValueError, match="EVALUATED_CONTEXT_PLAN"):
        adopt_context_plan(
            store,
            ContextEvaluationPlan.model_validate(payload),
            supersedes_plan_id=old.plan_id,
            registry_path=registry,
            declaration_path=registry,
            now=old.recorded_at,
        )


def test_teacher_scope_cannot_claim_independent_labels(tmp_path):
    from quantbot.learner.context_admission import ContextEvaluationPlan, ContextReadingScope

    _, _, _, old = setup_plan(tmp_path)
    with pytest.raises(ValueError, match="TARGET_MUST_AGREE"):
        ContextEvaluationPlan.model_validate(
            old.model_dump()
            | {
                "target": "MODEL_TEACHER_AGREEMENT_V1",
                "teacher_policy_id": "f" * 64,
                "reading_scope": ContextReadingScope(source_policy_id="a" * 64),
            }
        )
