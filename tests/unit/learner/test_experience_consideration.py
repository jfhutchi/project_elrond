"""Synthetic experience is considered once per revision, never auto-promoted."""

from datetime import timedelta
from decimal import Decimal
from importlib import import_module

import pytest

from quantbot.learner.experiment_lots import LotEvent, record_lot_event
from quantbot.learner.exploration_feedback import ExplorationFeedback
from tests.unit.learner.test_experience_continuity import api as continuity
from tests.unit.learner.test_experience_continuity import consent, transition_context
from tests.unit.learner.test_exploration_feedback import journal
from tests.unit.learner.test_exploration_feedback_revisions import cloned_source, late_flat_proof


def api():
    return import_module("quantbot.learner.experience_consideration")


def test_retries_consider_one_original_lot_and_record_explicit_no_change(tmp_path):
    store, policy, _, now = journal(tmp_path)
    first = api().consider_experience(store, policy, now=now)
    again = api().consider_experience(store, policy, now=now + timedelta(seconds=1))
    assert again == first
    assert first.status == "NO_CHANGE" and first.reason == "DESCRIPTIVE_FEEDBACK_NOT_FIT_AUTHORITY"
    assert first.verified_episodes == 1 and first.incomplete_episodes == 0
    assert first.grants_fit is first.grants_activation is False
    rows = api().current_considerations(store, policy, now=now)
    assert len(rows) == 1 and rows[0].net_pnl == Decimal("1.70")


def test_late_revision_replaces_current_consideration_not_trade_count(tmp_path):
    store, policy, _, now = journal(tmp_path)
    original = api().consider_experience(store, policy, now=now)
    later = late_flat_proof(store, policy, now)
    revised = api().consider_experience(store, policy, now=later)
    assert original.review_id != revised.review_id
    assert revised.verified_episodes == 1
    assert len(api().current_considerations(store, policy, now=later)) == 1
    assert api().consider_experience(store, policy, now=later + timedelta(seconds=1)) == revised
    assert api().current_considerations(store, policy, now=now)[0].recorded_at == now


def test_code_only_transition_preserves_original_attribution_and_progress(tmp_path):
    store, source, target, now = transition_context(tmp_path)
    api().consider_experience(store, source, now=now)
    proposal = continuity().propose_policy_transition(store, source, target, now=now)
    continuity().approve_policy_transition(
        store, proposal, approval=consent(proposal, now), now=now
    )
    review = api().consider_experience(store, target, now=now)
    assert review.verified_episodes == 1
    rows = api().current_considerations(store, target, now=now)
    assert len(rows) == 1 and rows[0].original_policy_id == source.policy_id
    assert len(api().lineage_feedback(store, target, now=now)) == 1


def test_missing_terminal_is_considered_incomplete_until_real_late_proof(tmp_path):
    store, policy, _, now = journal(tmp_path)
    isolated = cloned_source(store, policy, now, tmp_path / "missing.db", omit_terminal=True)
    missing = api().consider_experience(isolated, policy, now=now)
    assert missing.reason == "OWNED_OUTCOME_EVIDENCE_INCOMPLETE"
    assert missing.verified_episodes == 0 and missing.incomplete_episodes == 1
    later = now + timedelta(seconds=1)
    for event in store.read_as_of(policy.experiment_id, now):
        if event.kind == "LotEvent":
            row = event.decode(LotEvent)
            if row.kind == "ORDER_TERMINAL":
                record_lot_event(isolated, row.model_copy(update={"recorded_at": later}))
    complete = api().consider_experience(isolated, policy, now=later)
    assert complete.verified_episodes == 1 and complete.incomplete_episodes == 0
    assert complete.status == "NO_CHANGE"


def test_loss_and_flat_are_not_filtered_out(tmp_path):
    for name, fee, expected in [
        ("loss", Decimal("3"), Decimal("-1.10")),
        ("flat", Decimal("1.90"), Decimal("0.00")),
    ]:
        store, policy, _, now = journal(tmp_path / name, exit_fee=fee)
        review = api().consider_experience(store, policy, now=now)
        assert review.verified_episodes == 1
        assert api().current_considerations(store, policy, now=now)[0].net_pnl == expected


def test_late_conflicting_evidence_invalidates_current_input_and_preserves_history(tmp_path):
    store, policy, _, now = journal(tmp_path)
    api().consider_experience(store, policy, now=now)
    fill = next(
        e.decode(LotEvent)
        for e in store.read_as_of(policy.experiment_id, now)
        if e.kind == "LotEvent" and e.decode(LotEvent).kind == "FILL"
    )
    later = now + timedelta(seconds=1)
    record_lot_event(
        store,
        fill.model_copy(
            update={"fill_id": "unreconciled-extra", "occurred_at": later, "recorded_at": later}
        ),
    )
    assert api().current_considerations(store, policy, now=later) == ()
    rejected = api().consider_experience(store, policy, now=later)
    assert rejected.verified_episodes == 0 and rejected.incomplete_episodes == 1
    assert api().current_considerations(store, policy, now=now)[0].net_pnl == Decimal("1.70")


def clone_review_source(store, policy, now, path):
    isolated = cloned_source(store, policy, now, path)
    types = {
        "ExplorationFeedback": ExplorationFeedback,
        "ExperienceConsideration": api().ExperienceConsideration,
    }
    for index, event in enumerate(store.read_as_of(policy.experiment_id, now)):
        if event.kind in types:
            isolated.append(
                event.decode(types[event.kind]), idempotency_key="review-source:" + str(index)
            )
    return isolated


@pytest.mark.parametrize(
    "changes",
    [
        {"verified_episodes": 999},
        {"policy_id": "f" * 64},
        {"synthetic": False},
        {"reason": "NO_ELIGIBLE_EXPERIENCE"},
    ],
)
def test_hash_valid_forged_cached_review_is_not_returned(tmp_path, changes):
    store, policy, _, now = journal(tmp_path)
    review = api().consider_experience(store, policy, now=now)
    isolated = clone_review_source(store, policy, now, tmp_path / "forged-review.db")
    isolated.append(review.model_copy(update=changes), idempotency_key="forged-review")
    with pytest.raises(ValueError, match="REVIEW"):
        api().consider_experience(isolated, policy, now=now)


def test_consideration_cannot_predate_its_evidence(tmp_path):
    store, policy, _, now = journal(tmp_path)
    api().consider_experience(store, policy, now=now)
    row = api().current_considerations(store, policy, now=now)[0]
    isolated = cloned_source(store, policy, now, tmp_path / "backdated.db")
    for event in store.read_as_of(policy.experiment_id, now):
        if event.kind == "ExplorationFeedback":
            isolated.append(event.decode(ExplorationFeedback), idempotency_key="feedback")
    isolated.append(
        row.model_copy(update={"recorded_at": policy.recorded_at}),
        idempotency_key="backdated-consideration",
    )
    assert api().current_considerations(isolated, policy, now=now) == ()


def test_cached_target_review_cannot_predate_registered_approved_lineage(tmp_path):
    store, source, target, now = transition_context(tmp_path)
    earlier = now - timedelta(seconds=2)
    api().consider_experience(store, source, now=earlier)
    proposal = continuity().propose_policy_transition(store, source, target, now=now)
    continuity().approve_policy_transition(
        store, proposal, approval=consent(proposal, now), now=now
    )
    review = api().consider_experience(store, target, now=now)
    # Clone every source receipt except the current review, retaining real source timestamps.
    from quantbot.learner.experiment_store import ExperimentStore

    isolated = ExperimentStore(tmp_path / "backdated-target.db", create=True)
    models = {
        "ExperimentManifest": import_module(
            "quantbot.learner.experiment_contract"
        ).ExperimentManifest,
        "ExplorationPolicy": type(source),
        "ExplorationObservation": import_module(
            "quantbot.learner.exploration"
        ).ExplorationObservation,
        "LotEvent": LotEvent,
        "ExplorationFeedback": ExplorationFeedback,
        "ExperienceConsideration": api().ExperienceConsideration,
        "PolicyTransitionProposal": continuity().PolicyTransitionProposal,
        "PolicyContinuityApproval": continuity().PolicyContinuityApproval,
        "PolicyContinuityReceipt": continuity().PolicyContinuityReceipt,
    }
    for index, event in enumerate(store.read_as_of(source.experiment_id, now)):
        if event.kind in models:
            isolated.append(event.decode(models[event.kind]), idempotency_key=str(index))
    isolated.append(
        review.model_copy(update={"recorded_at": earlier}), idempotency_key="bad-review"
    )
    with pytest.raises(ValueError, match="REVIEW|REGISTERED"):
        api().consider_experience(isolated, target, now=now)


def test_competing_consideration_writer_fails_before_any_evidence_write(tmp_path):
    from quantbot.storage import AlreadyLockedError, SingleWriterLock

    store, policy, _, now = journal(tmp_path)
    before = store.path.read_bytes()
    with SingleWriterLock(store.path.with_name(store.path.name + ".experience.lock")):
        with pytest.raises(AlreadyLockedError):
            api().consider_experience(store, policy, now=now)
    assert store.path.read_bytes() == before
    first = api().consider_experience(store, policy, now=now)
    assert api().consider_experience(store, policy, now=now + timedelta(seconds=1)) == first
