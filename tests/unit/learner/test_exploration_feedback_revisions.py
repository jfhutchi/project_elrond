"""Synthetic owned outcomes: immutable revisions never mean more trades or authority."""
from datetime import timedelta
from decimal import Decimal
from importlib import import_module

import pytest

from quantbot.learner.experiment_contract import ExperimentManifest
from quantbot.learner.experiment_lots import LotEvent, record_lot_event, resolve_lot
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.exploration_feedback import (
    journal_exploration_feedback,
    verified_feedback,
)
from quantbot.learner.exploration_scorecard import exploration_scorecard
from tests.unit.learner.test_exploration_feedback import journal


def late_flat_proof(store, policy, now):
    flat = next(event.decode(LotEvent) for event in store.read_as_of(policy.experiment_id, now)
                if event.kind == 'LotEvent' and event.decode(LotEvent).kind == 'RECONCILED_FLAT')
    later = now + timedelta(seconds=1)
    record_lot_event(store, flat.model_copy(update={
        'occurred_at': later, 'recorded_at': later, 'source_hash': 'e'*64}))
    assert resolve_lot(store, flat.lot_id, now=later).status == 'COMPLETE'
    return later


def cloned_source(store, policy, now, path, *, omit_terminal=False):
    result = ExperimentStore(path, create=True)
    types = {
        'ExperimentManifest': ('experiment_contract', 'ExperimentManifest'),
        'ExplorationPolicy': ('exploration', 'ExplorationPolicy'),
        'ExplorationObservation': ('exploration', 'ExplorationObservation'),
        'LotEvent': ('experiment_lots', 'LotEvent'),
    }
    for index, event in enumerate(store.read_as_of(policy.experiment_id, now)):
        if event.kind in types:
            module, name = types[event.kind]
            model = getattr(import_module('quantbot.learner.'+module), name)
            row = event.decode(model)
            if omit_terminal and isinstance(row, LotEvent) and row.kind == 'ORDER_TERMINAL':
                continue
            result.append(row, idempotency_key='source:'+str(index))
    return result


def test_late_valid_source_gets_new_feedback_without_double_count_or_history_rewrite(tmp_path):
    store, policy, _, now = journal(tmp_path)
    original = journal_exploration_feedback(store, policy, now=now)[0]
    earlier = store.read_as_of(policy.experiment_id, now)
    later = late_flat_proof(store, policy, now)
    assert verified_feedback(store, policy, now=later) == ()
    revision = journal_exploration_feedback(store, policy, now=later)
    assert len(revision) == 1
    assert revision[0].lot_id == original.lot_id
    assert revision[0].source_state_hash != original.source_state_hash
    assert revision[0].net_pnl == original.net_pnl == Decimal('1.70')
    assert verified_feedback(store, policy, now=later) == revision
    assert journal_exploration_feedback(store, policy, now=later+timedelta(seconds=1)) == ()
    assert store.read_as_of(policy.experiment_id, now) == earlier
    assert verified_feedback(store, policy, now=now) == (original,)
    score = exploration_scorecard(store, policy, now=later)
    assert score.recorded_feedback == 2 and score.verified_episodes == 1
    assert score.net_pnl == original.net_pnl and score.excluded_feedback == 1
    assert score.predictor_fit is score.grants_execution is False


@pytest.mark.parametrize('changes', [
    {'net_pnl': Decimal('100')}, {'actual_fees': Decimal('0')},
    {'actual_net_return': Decimal('10')}, {'executed_gross_return': Decimal('10')},
    {'result': 'LOSS'}, {'observation_id': 'f'*64}, {'synthetic': False},
    {'hypothesis': 'ADVERSE_EXECUTED_PRICE_MOVE'},
    {'baseline_executed_return_differences': {'a'*64: Decimal('1')}},
])
def test_learning_reader_rejects_hash_valid_but_inconsistent_feedback(tmp_path, changes):
    store, policy, _, now = journal(tmp_path)
    feedback = journal_exploration_feedback(store, policy, now=now)[0]
    isolated = cloned_source(store, policy, now, tmp_path/'forged.db')
    isolated.append(feedback.model_copy(update=changes), idempotency_key='forged')
    assert verified_feedback(isolated, policy, now=now) == ()
    before = isolated.path.read_bytes()
    assert journal_exploration_feedback(isolated, policy, now=now) == ()
    assert isolated.path.read_bytes() == before


def test_duplicate_current_revision_is_not_two_experiences(tmp_path):
    store, policy, _, now = journal(tmp_path)
    feedback = journal_exploration_feedback(store, policy, now=now)[0]
    store.append(feedback, idempotency_key='unexpected-duplicate')
    assert verified_feedback(store, policy, now=now) == ()
    assert journal_exploration_feedback(store, policy, now=now) == ()


def test_late_invalid_fill_cannot_create_a_replacement_outcome(tmp_path):
    store, policy, _, now = journal(tmp_path)
    original = journal_exploration_feedback(store, policy, now=now)[0]
    fill = next(event.decode(LotEvent) for event in store.read_as_of(policy.experiment_id, now)
                if event.kind == 'LotEvent' and event.decode(LotEvent).kind == 'FILL')
    later = now+timedelta(seconds=1)
    record_lot_event(store, fill.model_copy(update={'fill_id':'late-unreconciled-fill',
        'occurred_at':later, 'recorded_at':later}))
    assert journal_exploration_feedback(store, policy, now=later) == ()
    assert verified_feedback(store, policy, now=later) == ()
    assert verified_feedback(store, policy, now=now) == (original,)


def test_code_only_policy_change_cannot_claim_previous_trade_experience(tmp_path):
    store, policy, _, now = journal(tmp_path)
    journal_exploration_feedback(store, policy, now=now)
    successor = policy.model_copy(update={'code_hash':'f'*64})
    store.append(successor, idempotency_key='successor-policy')
    assert verified_feedback(store, successor, now=now) == ()
    assert journal_exploration_feedback(store, successor, now=now) == ()


def test_legacy_feedback_key_remains_readable_and_can_receive_a_new_revision(tmp_path):
    store, policy, _, now = journal(tmp_path)
    feedback = journal_exploration_feedback(store, policy, now=now)[0]
    isolated = cloned_source(store, policy, now, tmp_path/'legacy.db')
    isolated.append(feedback, idempotency_key='exploration-feedback:'+feedback.lot_id)
    assert verified_feedback(isolated, policy, now=now) == (feedback,)
    later = late_flat_proof(isolated, policy, now)
    revised = journal_exploration_feedback(isolated, policy, now=later)
    assert len(revised) == 1 and revised[0].lot_id == feedback.lot_id
    assert verified_feedback(isolated, policy, now=later) == revised
    assert verified_feedback(isolated, policy, now=now) == (feedback,)


def test_missing_terminal_waits_until_genuine_late_evidence_completes_the_lot(tmp_path):
    store, policy, _, now = journal(tmp_path)
    isolated = cloned_source(store, policy, now, tmp_path/'late-terminal.db', omit_terminal=True)
    assert journal_exploration_feedback(isolated, policy, now=now) == ()
    later = now+timedelta(seconds=1)
    for event in store.read_as_of(policy.experiment_id, now):
        if event.kind == 'LotEvent':
            row = event.decode(LotEvent)
            if row.kind == 'ORDER_TERMINAL':
                record_lot_event(isolated, row.model_copy(update={'recorded_at':later}))
    feedback = journal_exploration_feedback(isolated, policy, now=later)
    assert len(feedback) == 1 and feedback[0].actual_fees == Decimal('.30')
    assert verified_feedback(isolated, policy, now=now) == ()
    assert verified_feedback(isolated, policy, now=later) == feedback


def test_later_cross_experiment_ownership_conflict_invalidates_current_feedback(tmp_path):
    store, policy, _, now = journal(tmp_path)
    original = journal_exploration_feedback(store, policy, now=now)[0]
    events = store.read_as_of(policy.experiment_id, now)
    manifest = next(event.decode(ExperimentManifest) for event in events
                    if event.kind == 'ExperimentManifest')
    later = now + timedelta(seconds=1)
    foreign = manifest.model_copy(update={'experiment_id': 'other-experiment'})
    store.append(foreign, idempotency_key='foreign-manifest')
    declaration = next(event.decode(LotEvent) for event in events
                       if event.kind == 'LotEvent'
                       and event.decode(LotEvent).kind == 'ENTRY_DECLARED')
    record_lot_event(store, declaration.model_copy(update={
        'experiment_id': foreign.experiment_id, 'recorded_at': later}))
    assert resolve_lot(store, original.lot_id, now=later).status == 'INCOMPLETE'
    assert verified_feedback(store, policy, now=later) == ()
    assert journal_exploration_feedback(store, policy, now=later) == ()
    assert verified_feedback(store, policy, now=now) == (original,)
