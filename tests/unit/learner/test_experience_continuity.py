"""Approved code-only continuity never relabels original trade evidence."""

from datetime import timedelta
from importlib import import_module

import pytest

from tests.unit.learner.test_exploration_feedback import journal


def api():
    return import_module("quantbot.learner.experience_continuity")


def transition_context(tmp_path):
    store, source, _, now = journal(tmp_path)
    target = source.model_copy(
        update={"code_hash": "f" * 64, "recorded_at": now + timedelta(seconds=1)}
    )
    store.append(target, idempotency_key="new-policy")
    return store, source, target, now + timedelta(seconds=2)


def consent(proposal, now, **changes):
    return api().PolicyContinuityApproval(
        experiment_id=proposal.experiment_id,
        proposal_id=proposal.proposal_id,
        recorded_at=now,
        operator="jfhutchi",
        reference="synthetic:test",
        authority="SYNTHETIC_TEST_ONLY",
        synthetic=True,
        **changes,
    )


def test_registered_code_only_transition_retains_original_policy(tmp_path):
    store, source, target, now = transition_context(tmp_path)
    proposal = api().propose_policy_transition(store, source, target, now=now)
    assert api().verified_policy_lineage(store, target, now=now) == (target,)
    approval = consent(proposal, now)
    receipt = api().approve_policy_transition(store, proposal, approval=approval, now=now)
    assert api().verified_policy_lineage(store, target, now=now) == (source, target)
    assert api().approve_policy_transition(store, proposal, approval=approval, now=now) == receipt
    assert api().verified_policy_lineage(store, target, now=target.recorded_at) == (target,)
    assert source.policy_id != target.policy_id


@pytest.mark.parametrize(
    "changes", [{"maximum_position_bps": 25}, {"manifest_hash": "b" * 64}, {"synthetic": False}]
)
def test_semantic_policy_change_never_inherits_experience(tmp_path, changes):
    store, source, target, now = transition_context(tmp_path)
    incompatible = target.model_copy(update=changes)
    store.append(incompatible, idempotency_key="incompatible-policy")
    with pytest.raises(ValueError, match="COMPATIBLE|REGISTERED"):
        api().propose_policy_transition(store, source, incompatible, now=now)


def test_wrong_approval_and_unregistered_policy_fail_closed(tmp_path):
    store, source, target, now = transition_context(tmp_path)
    proposal = api().propose_policy_transition(store, source, target, now=now)
    wrong = consent(proposal, now).model_copy(update={"proposal_id": "a" * 64})
    with pytest.raises(ValueError, match="EXACT"):
        api().approve_policy_transition(store, proposal, approval=wrong, now=now)
    unknown = target.model_copy(update={"code_hash": "e" * 64})
    with pytest.raises(ValueError, match="REGISTERED"):
        api().propose_policy_transition(store, source, unknown, now=now)


def test_ambiguous_parent_is_refused(tmp_path):
    store, source, target, now = transition_context(tmp_path)
    proposal = api().propose_policy_transition(store, source, target, now=now)
    api().approve_policy_transition(store, proposal, approval=consent(proposal, now), now=now)
    other = source.model_copy(update={"code_hash": "d" * 64})
    store.append(other, idempotency_key="other-policy")
    alternate = api().propose_policy_transition(store, other, target, now=now)
    with pytest.raises(ValueError, match="PARENT"):
        api().approve_policy_transition(store, alternate, approval=consent(alternate, now), now=now)


def test_registered_but_forged_proposal_cannot_be_approved(tmp_path):
    store, source, target, now = transition_context(tmp_path)
    proposal = api().propose_policy_transition(store, source, target, now=now)
    forged = proposal.model_copy(update={"semantics_hash": "c" * 64})
    store.append(forged, idempotency_key="forged-proposal")
    with pytest.raises(ValueError, match="EXACT"):
        api().approve_policy_transition(store, forged, approval=consent(forged, now), now=now)


def test_backdated_lineage_proposal_is_rejected_by_reader(tmp_path):
    store, source, target, now = transition_context(tmp_path)
    proposal = (
        api()
        .propose_policy_transition(store, source, target, now=now)
        .model_copy(update={"recorded_at": target.recorded_at - timedelta(seconds=1)})
    )
    approval = consent(proposal, now)
    receipt = api().PolicyContinuityReceipt(
        experiment_id=target.experiment_id,
        recorded_at=now,
        proposal_id=proposal.proposal_id,
        approval_id=approval.approval_id,
        source_policy_id=source.policy_id,
        target_policy_id=target.policy_id,
        synthetic=True,
    )
    for record in (proposal, approval, receipt):
        store.append(record, idempotency_key="injected:" + type(record).__name__)
    with pytest.raises(ValueError, match="REGISTERED"):
        api().verified_policy_lineage(store, target, now=now)
