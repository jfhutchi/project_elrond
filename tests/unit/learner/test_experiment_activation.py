"""Exact operator approvals never transfer to successor artifacts or proposals."""

from importlib import import_module
from types import SimpleNamespace

import pytest

from quantbot.learner.experiment_contract import CandidateArtifact
from quantbot.learner.experiment_research import activation_package, run_registered_fit
from quantbot.learner.experiment_store import ConsentNotice, ExperimentStore
from quantbot.learner.paper import PaperModelBinding
from quantbot.research.manifest import content_id
from tests.fixtures.adaptive_paper import manifest
from tests.unit.learner.test_experiment_research import Charger, Guard, fake_artifact, request


def module():
    return import_module("quantbot.learner.experiment_activation")


@pytest.fixture
def context(tmp_path, monkeypatch):
    frozen = manifest()
    store = ExperimentStore(tmp_path / "activation.db", create=True)
    store.append(frozen, idempotency_key="manifest")
    fit = run_registered_fit(
        store,
        frozen,
        request(),
        guard=Guard(),
        charger=Charger(),
        now=frozen.registered_at,
        synthetic_engine=fake_artifact,
    )
    candidate = next(
        e.decode(CandidateArtifact)
        for e in store.read_as_of(frozen.experiment_id, frozen.registered_at)
        if e.kind == "CandidateArtifact"
    )
    binding = PaperModelBinding.from_config(
        model_id=candidate.model_id,
        artifact_hash=content_id(candidate.artifact),
        code_hash=frozen.code_hash,
        execution_config={"synthetic": True},
    )
    package = activation_package(
        store,
        frozen,
        fit=fit,
        now=frozen.registered_at,
        config_hash=binding.configuration_hash,
        owner="synthetic-operator",
        rollback_model_id=None,
    )
    broker = SimpleNamespace(environment="PAPER", provider="synthetic")
    services = SimpleNamespace(
        runner=SimpleNamespace(
            model=SimpleNamespace(model_id=candidate.model_id),
            binding=binding,
            _broker=broker,
            _database=SimpleNamespace(path=tmp_path / "not-an-operational-store.db"),
            _identity=binding.strategy_identity(
                git_commit="a" * 40, deployed_at=candidate.recorded_at
            ),
            _clock=lambda: frozen.registered_at,
            clock_health=lambda now: True,
        ),
    )
    return store, frozen, package, services


def approval(package, frozen, **changes):
    return module().ExactArtifactApproval(
        experiment_id=frozen.experiment_id,
        recorded_at=frozen.registered_at,
        package_id=package.package_id,
        model_id=package.model_id,
        operator=package.owner,
        authority="SYNTHETIC_TEST_ONLY",
        reference="explicit isolated synthetic test",
        synthetic=True,
        **changes,
    )


def test_communication_consent_cannot_be_used_as_activation(context):
    store, frozen, package, services = context
    notice = ConsentNotice(
        experiment_id=frozen.experiment_id, consent_id="notice", recorded_at=frozen.registered_at
    )
    with pytest.raises(ValueError):
        module().activate_exact_package(
            store, frozen, package, approval=notice, services=services, simulation=True
        )
    assert store.state(frozen.experiment_id).activation_id is None


def test_missing_operational_gate_never_activates_sidecar(context):
    store, frozen, package, services = context
    with pytest.raises(ValueError, match="OPERATIONAL"):
        module().activate_exact_package(
            store,
            frozen,
            package,
            approval=approval(package, frozen),
            services=services,
            simulation=True,
        )
    assert store.state(frozen.experiment_id).activation_id is None


def test_different_model_or_package_cannot_inherit_approval(context):
    store, frozen, package, services = context
    consent = approval(package, frozen).model_copy(update={"model_id": "a" * 64})
    with pytest.raises(ValueError, match="EXACT_OPERATOR"):
        module().activate_exact_package(
            store,
            frozen,
            package,
            approval=consent,
            services=services,
            simulation=True,
        )
    assert store.state(frozen.experiment_id).activation_id is None


def test_verified_synthetic_approval_records_exact_active_model_once(context, monkeypatch):
    store, frozen, package, services = context
    monkeypatch.setattr(
        module(), "assess_paper_entry", lambda *args, **kwargs: SimpleNamespace(allowed=True)
    )
    consent = approval(package, frozen)
    activated = module().activate_exact_package(
        store,
        frozen,
        package,
        approval=consent,
        services=services,
        simulation=True,
    )
    state = store.state(frozen.experiment_id)
    assert state.state == "ACTIVE_PAPER"
    assert state.activation_id == activated.activation_id
    assert activated.model_id == package.model_id
    assert module().active_artifact(store, frozen, now=frozen.registered_at) == activated
    again = module().activate_exact_package(
        store,
        frozen,
        package,
        approval=consent,
        services=services,
        simulation=True,
    )
    assert again == activated
    assert store.state(frozen.experiment_id).revision == state.revision


def test_synthetic_approval_is_rejected_on_real_route(context):
    store, frozen, package, services = context
    with pytest.raises(ValueError, match="SYNTHETIC"):
        module().activate_exact_package(
            store,
            frozen,
            package,
            approval=approval(package, frozen),
            services=services,
        )
    assert store.state(frozen.experiment_id).activation_id is None
