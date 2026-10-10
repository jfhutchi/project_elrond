"""Synthetic staged switches preserve evidence and require exact authority."""

from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace

import pytest

from quantbot.learner.experiment_activation import activate_exact_package, active_artifact
from quantbot.learner.experiment_contract import CandidateArtifact
from quantbot.learner.experiment_lots import LotEvent, record_lot_event
from quantbot.learner.experiment_research import activation_package, run_registered_fit
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.paper import PaperModelBinding
from quantbot.research.manifest import content_id
from tests.unit.learner.test_experiment_activation import approval, context  # noqa: F401
from tests.unit.learner.test_experiment_lots import prepare
from tests.unit.learner.test_experiment_research import Charger, Guard, fake_artifact, request


def api():
    return import_module("quantbot.learner.experiment_switch")


@pytest.fixture
def switch_context(context, monkeypatch):  # noqa: F811 - imported pytest fixture
    store, frozen, incumbent, old_services = context
    fit = run_registered_fit(
        store,
        frozen,
        request("constant"),
        guard=Guard(),
        charger=Charger(),
        now=frozen.registered_at,
        synthetic_engine=fake_artifact,
    )
    candidate = next(
        e.decode(CandidateArtifact)
        for e in store.read_as_of(frozen.experiment_id, frozen.registered_at)
        if e.kind == "CandidateArtifact"
        and e.decode(CandidateArtifact).model_id == fit.candidate_id
    )
    binding = PaperModelBinding.from_config(
        model_id=candidate.model_id,
        artifact_hash=content_id(candidate.artifact),
        code_hash=frozen.code_hash,
        execution_config={"synthetic": True},
    )
    target = activation_package(
        store,
        frozen,
        fit=fit,
        now=frozen.registered_at,
        config_hash=binding.configuration_hash,
        owner=incumbent.owner,
        rollback_model_id=incumbent.model_id,
    )
    activation_module = import_module("quantbot.learner.experiment_activation")
    monkeypatch.setattr(
        activation_module,
        "assess_paper_entry",
        lambda *args, **kwargs: SimpleNamespace(allowed=True),
    )
    activated = activate_exact_package(
        store,
        frozen,
        incumbent,
        approval=approval(incumbent, frozen),
        services=old_services,
        simulation=True,
    )
    clock = [frozen.registered_at + timedelta(minutes=1)]
    old_services.runner._clock = lambda: clock[0]
    new_services = SimpleNamespace(runner=SimpleNamespace(**vars(old_services.runner)))
    new_services.runner.binding = binding
    new_services.runner.model = SimpleNamespace(model_id=target.model_id)
    new_services.runner._identity = binding.strategy_identity(
        git_commit="a" * 40, deployed_at=frozen.registered_at
    )
    return store, frozen, incumbent, target, old_services, new_services, activated, clock


def proposal_and_consent(ctx, *, purpose="SWITCH", target=None):
    store, frozen, _, proposed, _, _, _, clock = ctx
    proposal = api().propose_artifact_switch(
        store,
        frozen,
        target or proposed,
        expected_revision=store.state(frozen.experiment_id).revision,
        now=clock[0],
        purpose=purpose,
    )
    consent = api().ExactSwitchApproval(
        experiment_id=frozen.experiment_id,
        proposal_id=proposal.proposal_id,
        recorded_at=clock[0],
        operator=proposed.owner,
        authority="SYNTHETIC_TEST_ONLY",
        reference="synthetic:test",
        synthetic=True,
    )
    return proposal, consent


def stage(ctx):
    store, frozen, _, _, old, _, _, _ = ctx
    proposal, consent = proposal_and_consent(ctx)
    return api().stage_artifact_switch(
        store, frozen, proposal, approval=consent, services=old, simulation=True
    )


def test_stage_fences_entries_and_restart_requires_exact_target_binding(switch_context):
    store, frozen, _, target, old, new, _, clock = switch_context
    staged = stage(switch_context)
    assert store.state(frozen.experiment_id).state == "ACTIVATION_REQUIRED"
    assert active_artifact(store, frozen, now=clock[0]) is None
    restarted = ExperimentStore(store.path)
    with pytest.raises(ValueError, match="BINDING"):
        api().complete_artifact_switch(restarted, frozen, staged, services=old, simulation=True)
    assert active_artifact(restarted, frozen, now=clock[0]) is None
    activated = api().complete_artifact_switch(
        restarted, frozen, staged, services=new, simulation=True
    )
    assert active_artifact(store, frozen, now=clock[0]) == activated
    assert activated.model_id == target.model_id
    assert (
        api().complete_artifact_switch(store, frozen, staged, services=new, simulation=True)
        == activated
    )


def test_wrong_consent_or_stale_incumbent_cannot_stage(switch_context):
    store, frozen, _, _, old, _, incumbent, clock = switch_context
    proposal, consent = proposal_and_consent(switch_context)
    wrong = consent.model_copy(update={"proposal_id": "f" * 64})
    with pytest.raises(ValueError, match="EXACT"):
        api().stage_artifact_switch(
            store, frozen, proposal, approval=wrong, services=old, simulation=True
        )
    store.transition(
        frozen.experiment_id,
        expected_revision=proposal.incumbent_revision,
        target="EVALUATING",
        reason="changed-state",
        now=clock[0],
    )
    with pytest.raises(ValueError, match="INCUMBENT"):
        api().stage_artifact_switch(
            store, frozen, proposal, approval=consent, services=old, simulation=True
        )
    assert store.state(frozen.experiment_id).activation_id == incumbent.activation_id


@pytest.mark.parametrize("fence", ["pending", "inventory"])
def test_pending_intent_or_incomplete_owned_lot_blocks_switch(switch_context, tmp_path, fence):
    store, frozen, _, _, old, _, _, clock = switch_context
    proposal, consent = proposal_and_consent(switch_context)
    if fence == "pending":
        store.enqueue_intent(frozen.experiment_id, intent_id="unknown-intent", document="{}")
    else:
        original = prepare(tmp_path / "lot")
        declaration = next(
            e.decode(LotEvent)
            for e in original.read_as_of(frozen.experiment_id, clock[0] + timedelta(days=10))
            if e.kind == "LotEvent" and e.decode(LotEvent).kind == "ENTRY_DECLARED"
        )
        record_lot_event(
            store, declaration.model_copy(update={"occurred_at": clock[0], "recorded_at": clock[0]})
        )
    with pytest.raises(ValueError, match="PENDING|OWNED"):
        api().stage_artifact_switch(
            store, frozen, proposal, approval=consent, services=old, simulation=True
        )
    assert store.state(frozen.experiment_id).state == "ACTIVE_PAPER"


def test_generic_activation_and_transition_cannot_bypass_pending_switch(switch_context):
    store, frozen, incumbent, _, old, _, _, clock = switch_context
    stage(switch_context)
    with pytest.raises(ValueError, match="SWITCH"):
        activate_exact_package(
            store,
            frozen,
            incumbent,
            approval=approval(incumbent, frozen),
            services=old,
            simulation=True,
        )
    with pytest.raises(ValueError, match="SWITCH"):
        store.transition(
            frozen.experiment_id,
            expected_revision=store.state(frozen.experiment_id).revision,
            target="ACTIVE_PAPER",
            reason="bypass",
            now=clock[0],
        )


def test_failed_completion_is_atomic_and_stays_fenced(switch_context, monkeypatch):
    store, frozen, _, _, _, new, _, clock = switch_context
    staged = stage(switch_context)
    before = store.read_as_of(frozen.experiment_id, clock[0])
    original = api()._insert_record

    def fail(db, record, key):
        if type(record).__name__ == "ArtifactActivation":
            raise RuntimeError("synthetic commit fault")
        original(db, record, key)

    monkeypatch.setattr(api(), "_insert_record", fail)
    with pytest.raises(RuntimeError, match="fault"):
        api().complete_artifact_switch(store, frozen, staged, services=new, simulation=True)
    assert store.read_as_of(frozen.experiment_id, clock[0]) == before
    assert active_artifact(store, frozen, now=clock[0]) is None
    monkeypatch.setattr(api(), "_insert_record", original)
    assert (
        api()
        .complete_artifact_switch(store, frozen, staged, services=new, simulation=True)
        .model_id
        == new.runner.model.model_id
    )


def test_exact_rollback_preserves_newer_evidence_and_charged_trials(switch_context):
    store, frozen, incumbent, _, old, new, _, clock = switch_context
    staged = stage(switch_context)
    api().complete_artifact_switch(store, frozen, staged, services=new, simulation=True)
    before = store.read_as_of(frozen.experiment_id, clock[0])
    clock[0] += timedelta(seconds=1)
    proposal, consent = proposal_and_consent(switch_context, purpose="ROLLBACK", target=incumbent)
    rollback = api().stage_artifact_switch(
        store, frozen, proposal, approval=consent, services=new, simulation=True
    )
    recovered = api().complete_artifact_switch(
        store, frozen, rollback, services=old, simulation=True
    )
    assert recovered.model_id == incumbent.model_id
    assert store.read_as_of(frozen.experiment_id, before[-1].recorded_at) == before
    assert sum(e.kind == "FitStart" for e in store.read_as_of(frozen.experiment_id, clock[0])) == 2


def test_synthetic_switch_cannot_use_real_route(switch_context):
    store, frozen, _, _, old, _, _, _ = switch_context
    proposal, consent = proposal_and_consent(switch_context)
    with pytest.raises(ValueError, match="SYNTHETIC"):
        api().stage_artifact_switch(store, frozen, proposal, approval=consent, services=old)


def test_explicit_pending_recovery_restores_only_retained_incumbent(switch_context):
    store, frozen, incumbent, _, old, new, _, clock = switch_context
    original = stage(switch_context)
    before = store.read_as_of(frozen.experiment_id, clock[0])
    clock[0] += timedelta(seconds=1)
    proposal, consent = proposal_and_consent(
        switch_context, purpose="RECOVER_PENDING", target=incumbent
    )
    recovery = api().stage_artifact_switch(
        store, frozen, proposal, approval=consent, services=old, simulation=True
    )
    with pytest.raises(ValueError, match="STALE"):
        api().complete_artifact_switch(store, frozen, original, services=new, simulation=True)
    activated = api().complete_artifact_switch(
        store, frozen, recovery, services=old, simulation=True
    )
    assert activated.model_id == incumbent.model_id
    assert store.read_as_of(frozen.experiment_id, before[-1].recorded_at) == before


def test_stage_fault_does_not_leave_partial_approval_or_fence(switch_context, monkeypatch):
    store, frozen, _, _, old, _, _, clock = switch_context
    proposal, consent = proposal_and_consent(switch_context)
    before = store.read_as_of(frozen.experiment_id, clock[0])

    def fail(*args):
        raise RuntimeError("synthetic stage fault")

    monkeypatch.setattr(api(), "_save_transition", fail)
    with pytest.raises(RuntimeError, match="stage fault"):
        api().stage_artifact_switch(
            store, frozen, proposal, approval=consent, services=old, simulation=True
        )
    assert store.read_as_of(frozen.experiment_id, clock[0]) == before
    assert store.state(frozen.experiment_id).state == "ACTIVE_PAPER"


@pytest.mark.parametrize("failure", ["clock", "gate"])
def test_completion_gate_failure_keeps_pending_fence(switch_context, monkeypatch, failure):
    store, frozen, _, _, _, new, _, clock = switch_context
    staged = stage(switch_context)
    if failure == "clock":
        new.runner.clock_health = lambda now: False
    else:
        monkeypatch.setattr(
            import_module("quantbot.learner.experiment_activation"),
            "assess_paper_entry",
            lambda *args, **kwargs: SimpleNamespace(allowed=False),
        )
    with pytest.raises(ValueError, match="CLOCK|OPERATIONAL"):
        api().complete_artifact_switch(store, frozen, staged, services=new, simulation=True)
    assert active_artifact(store, frozen, now=clock[0]) is None


def test_evidence_race_aborts_completion(switch_context, monkeypatch):
    store, frozen, _, _, _, new, _, clock = switch_context
    staged = stage(switch_context)
    original = api()._validate_activation_request

    def concurrent_update(*args, **kwargs):
        result = original(*args, **kwargs)
        from quantbot.learner.experiment_store import ConsentNotice

        store.append(
            ConsentNotice(
                experiment_id=frozen.experiment_id,
                recorded_at=clock[0],
                consent_id="concurrent-change",
            ),
            idempotency_key="race",
        )
        return result

    monkeypatch.setattr(api(), "_validate_activation_request", concurrent_update)
    with pytest.raises(ValueError, match="EVIDENCE_CHANGED"):
        api().complete_artifact_switch(store, frozen, staged, services=new, simulation=True)
    assert active_artifact(store, frozen, now=clock[0]) is None


def test_generic_reactivation_from_evaluating_cannot_bypass_switch(switch_context):
    store, frozen, _, target, _, new, incumbent, clock = switch_context
    store.transition(
        frozen.experiment_id,
        expected_revision=store.state(frozen.experiment_id).revision,
        target="EVALUATING",
        reason="evaluation",
        now=clock[0],
    )
    store.enqueue_intent(frozen.experiment_id, intent_id="unresolved", document="{}")
    with pytest.raises(ValueError, match="SWITCH"):
        activate_exact_package(
            store, frozen, target, approval=approval(target, frozen), services=new, simulation=True
        )
    assert store.state(frozen.experiment_id).activation_id == incumbent.activation_id
    assert store.state(frozen.experiment_id).state == "EVALUATING"


def test_pending_switch_cannot_start_new_research_or_spend_trial(switch_context):
    store, frozen, _, _, _, _, incumbent, clock = switch_context
    stage(switch_context)
    proposed = request().model_copy(update={"generation": 1, "parent_model_id": incumbent.model_id})
    store.append(proposed, idempotency_key="unstarted-request")
    research = import_module("quantbot.learner.experiment_research")
    assert research.research_eligibility(store, frozen, now=clock[0]) is None
    charger = Charger()
    with pytest.raises(ValueError, match="SWITCH"):
        run_registered_fit(
            store,
            frozen,
            proposed,
            guard=Guard(),
            charger=charger,
            now=clock[0],
            synthetic_engine=fake_artifact,
        )
    assert charger.calls == []
    result = import_module("quantbot.learner.experiment_adaptation").schedule_next_successor(
        store, frozen, now=clock[0], guard=Guard()
    )
    assert result.status == "NORMAL_WAIT" and result.reason == "EXACT_SWITCH_PENDING"
