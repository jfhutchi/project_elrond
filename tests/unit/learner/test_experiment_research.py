"""Frozen pooled research charges trials before engine calls, including failures."""

from datetime import UTC, datetime, timedelta
from importlib import import_module

import pytest

from quantbot.learner.experiment_observation import build_observation_batch, resolve_batch_labels
from quantbot.learner.experiment_store import ExperimentStore
from tests.fixtures.adaptive_paper import manifest, snapshot


def module():
    return import_module("quantbot.learner.experiment_research")


class Guard:
    def __init__(self, permitted=True):
        self.permitted = permitted
        self.calls = []

    def permits(self, start, end):
        self.calls.append((start, end))
        return self.permitted


class Charger:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def used_calls(self, experiment_id):
        return len(self.calls)

    def charge(self, trial_id, manifest_hash):
        self.calls.append((trial_id, manifest_hash))
        if self.fail:
            raise ValueError("CANONICAL_CHARGE_REFUSED")


def inputs():
    observation = build_observation_batch(manifest(), snapshot(), now=snapshot().received_at)
    originals = resolve_batch_labels(observation, snapshot(65), now=snapshot(65).received_at)

    def rows(start, count):
        dates = []
        while len(dates) < count:
            if start.weekday() < 5:
                dates.append(start)
            start += timedelta(days=1)
        result = []
        for day in dates:
            for original in originals:
                delta = day - original.features.feature_at
                features = original.features.model_copy(
                    update={
                        field: getattr(original.features, field) + delta
                        for field in ("feature_at", "decision_at", "target_at", "receipt_at")
                    }
                )
                result.append(
                    original.model_copy(
                        update={
                            "features": features,
                            "available_at": original.available_at + delta,
                        }
                    )
                )
        return tuple(result)

    return (
        rows(datetime(2026, 2, 2, 15, tzinfo=UTC), 20),
        rows(datetime(2026, 3, 16, 15, tzinfo=UTC), 5),
    )


@pytest.fixture
def context(tmp_path):
    store = ExperimentStore(tmp_path / "research.db", create=True)
    store.append(manifest(), idempotency_key="manifest")
    return store, inputs()


def request(family="ridge", **kwargs):
    training, validation = inputs()
    return module().prepare_fit_request(
        manifest(),
        family=family,
        training=training,
        validation=validation,
        now=manifest().registered_at,
        guard=Guard(),
        **kwargs,
    )


def test_pooled_symbols_do_not_multiply_session_counts():
    training, validation = inputs()
    with pytest.raises(ValueError, match="TRAINING_SESSIONS"):
        module().prepare_fit_request(
            manifest(),
            family="ridge",
            training=training[:6],
            validation=validation,
            now=manifest().registered_at,
            guard=Guard(),
        )
    prepared = request()
    assert prepared.training_sessions == 20
    assert prepared.validation_sessions == 5


def test_protected_evidence_refused_before_request():
    training, validation = inputs()
    with pytest.raises(ValueError, match="PROTECTED"):
        module().prepare_fit_request(
            manifest(),
            family="ridge",
            training=training,
            validation=validation,
            now=manifest().registered_at,
            guard=Guard(False),
        )


def test_charge_precedes_engine_and_replay_never_refits(context):
    store, _ = context
    charger = Charger()
    calls = []

    def engine(prepared):
        assert len(charger.calls) == 1
        calls.append(prepared.request_id)
        raise RuntimeError("synthetic engine failure")

    prepared = request()
    receipt = module().run_registered_fit(
        store,
        manifest(),
        prepared,
        guard=Guard(),
        charger=charger,
        now=manifest().registered_at,
        synthetic_engine=engine,
    )
    assert receipt.status == "FAILED"
    assert receipt.charged is True
    again = module().run_registered_fit(
        store,
        manifest(),
        prepared,
        guard=Guard(),
        charger=charger,
        now=manifest().registered_at + timedelta(minutes=1),
        synthetic_engine=engine,
    )
    assert again == receipt
    assert len(calls) == len(charger.calls) == 1


def test_charge_failure_never_invokes_engine(context):
    store, _ = context
    calls = []
    receipt = module().run_registered_fit(
        store,
        manifest(),
        request(),
        guard=Guard(),
        charger=Charger(True),
        now=manifest().registered_at,
        synthetic_engine=lambda row: calls.append(row),
    )
    assert receipt.status == "CHARGE_REFUSED"
    assert not calls


def test_substituted_manifest_and_unmatured_input_are_rejected():
    training, validation = inputs()
    invalid = validation[0].model_copy(
        update={"available_at": manifest().registered_at + timedelta(seconds=1)}
    )
    with pytest.raises(ValueError, match="FUTURE"):
        module().prepare_fit_request(
            manifest(),
            family="ridge",
            training=training,
            validation=(invalid, *validation[1:]),
            now=manifest().registered_at,
            guard=Guard(),
        )


def test_real_request_cannot_use_in_process_engine(context):
    store, _ = context
    real = manifest().model_copy(update={"synthetic": False})
    prepared = request().model_copy(update={"synthetic": False})
    with pytest.raises(ValueError, match="SYNTHETIC_ENGINE"):
        module().run_registered_fit(
            store,
            real,
            prepared,
            guard=Guard(),
            charger=Charger(),
            now=real.registered_at,
            synthetic_engine=lambda row: None,
        )


def fake_artifact(prepared):
    from quantbot.learner.estimators import FittedEstimator
    from quantbot.learner.five_minute import FEATURE_NAMES

    family = "mean" if prepared.family == "constant" else "ridge"
    return FittedEstimator(
        family=family,
        feature_names=FEATURE_NAMES,
        means=(0.0,) * 10,
        scales=(1.0,) * 10,
        coefficients=(0.0,) * 10,
        intercept=0.001,
        alpha=1.0,
        training_mse=0.0,
        seed=0,
        algorithm_version="mean-v1" if family == "mean" else "ridge-cholesky-v1",
    )


def test_artifact_available_only_after_actual_completion(context):
    store, _ = context
    start = manifest().registered_at
    completed = start + timedelta(minutes=3)
    receipt = module().run_registered_fit(
        store,
        manifest(),
        request(),
        guard=Guard(),
        charger=Charger(),
        now=start,
        synthetic_engine=fake_artifact,
        clock=lambda: completed,
    )
    assert receipt.recorded_at == completed
    assert receipt.status == "SUCCEEDED"
    assert not any(
        e.kind == "CandidateArtifact" for e in store.read_as_of(manifest().experiment_id, start)
    )
    artifact = next(
        e
        for e in store.read_as_of(manifest().experiment_id, completed)
        if e.kind == "CandidateArtifact"
    )
    assert artifact.recorded_at == completed


def test_same_initial_family_cannot_be_retuned_with_new_rows(context):
    store, (training, validation) = context
    now = manifest().registered_at
    charger = Charger()
    module().run_registered_fit(
        store,
        manifest(),
        request(),
        guard=Guard(),
        charger=charger,
        now=now,
        synthetic_engine=fake_artifact,
    )
    changed = training[0].model_copy(
        update={
            "raw_return": training[0].raw_return + 0.01,
            "excess_return": (training[0].raw_return + 0.01) - training[0].benchmark_return,
        }
    )
    another = module().prepare_fit_request(
        manifest(),
        family="ridge",
        training=(changed, *training[1:]),
        validation=validation,
        now=now,
        guard=Guard(),
    )
    with pytest.raises(ValueError, match="INITIAL_FAMILY_ALREADY_STARTED"):
        module().run_registered_fit(
            store,
            manifest(),
            another,
            guard=Guard(),
            charger=charger,
            now=now,
            synthetic_engine=fake_artifact,
        )
    assert len(charger.calls) == 1


def test_canonical_six_call_budget_blocks_engine(context):
    store, _ = context
    charger = Charger()
    charger.calls = [(str(index), "a" * 64) for index in range(6)]
    calls = []
    with pytest.raises(ValueError, match="SIX_CALL"):
        module().run_registered_fit(
            store,
            manifest(),
            request(),
            guard=Guard(),
            charger=charger,
            now=manifest().registered_at,
            synthetic_engine=lambda row: calls.append(row),
        )
    assert not calls


def test_unknown_started_call_does_not_claim_known_charge(context):
    store, _ = context
    prepared = request()
    store.append(
        module().FitStart(
            experiment_id=manifest().experiment_id,
            recorded_at=manifest().registered_at,
            request_id=prepared.request_id,
            trial_id=prepared.trial_id,
            manifest_hash=prepared.manifest_hash,
            synthetic=True,
        ),
        idempotency_key="fit-start:" + prepared.request_id,
    )
    receipt = module().run_registered_fit(
        store,
        manifest(),
        prepared,
        guard=Guard(),
        charger=Charger(),
        now=manifest().registered_at,
        synthetic_engine=fake_artifact,
    )
    assert receipt.status == "STARTED_UNRESOLVED"
    assert receipt.charged is None


def test_successor_requires_new_validation_and_five_future_sessions(context):
    store, (training, validation) = context
    now = manifest().registered_at
    charger = Charger()
    first = module().run_registered_fit(
        store,
        manifest(),
        request(),
        guard=Guard(),
        charger=charger,
        now=now,
        synthetic_engine=fake_artifact,
    )
    reused = request(generation=1, parent_model_id=first.candidate_id)
    with pytest.raises(ValueError, match="SEEN_VALIDATION"):
        module().run_registered_fit(
            store,
            manifest(),
            reused,
            guard=Guard(),
            charger=charger,
            now=now,
            synthetic_engine=fake_artifact,
        )
    fresh = []
    days = (7, 8, 9, 12, 13)
    for index, row in enumerate(validation):
        cutoff = datetime(2026, 10, days[index // 6], 15, tzinfo=UTC)
        delta = cutoff - row.features.feature_at
        features = row.features.model_copy(
            update={
                field: getattr(row.features, field) + delta
                for field in ("feature_at", "decision_at", "target_at", "receipt_at")
            }
        )
        fresh.append(
            row.model_copy(update={"features": features, "available_at": row.available_at + delta})
        )
    later = datetime(2026, 10, 13, 20, tzinfo=UTC)
    successor = module().prepare_fit_request(
        manifest(),
        family="ridge",
        training=training,
        validation=fresh,
        now=later,
        guard=Guard(),
        generation=1,
        parent_model_id=first.candidate_id,
    )
    result = module().run_registered_fit(
        store,
        manifest(),
        successor,
        guard=Guard(),
        charger=charger,
        now=later,
        synthetic_engine=fake_artifact,
    )
    assert result.status == "SUCCEEDED"
    assert result.candidate_id != first.candidate_id
    assert store.state(manifest().experiment_id).activation_id is None


def test_timeout_is_charged_and_never_retried(context):
    store, _ = context
    charger = Charger()

    def timeout(row):
        raise TimeoutError()

    receipt = module().run_registered_fit(
        store,
        manifest(),
        request("constant"),
        guard=Guard(),
        charger=charger,
        now=manifest().registered_at,
        synthetic_engine=timeout,
    )
    assert receipt.status == "TIMEOUT"
    assert receipt.charged is True
    again = module().run_registered_fit(
        store,
        manifest(),
        request("constant"),
        guard=Guard(),
        charger=charger,
        now=manifest().registered_at,
        synthetic_engine=fake_artifact,
    )
    assert again == receipt
    assert len(charger.calls) == 1


def test_activation_package_is_exact_proposal_never_consent(context):
    store, _ = context
    now = manifest().registered_at
    fit = module().run_registered_fit(
        store,
        manifest(),
        request(),
        guard=Guard(),
        charger=Charger(),
        now=now,
        synthetic_engine=fake_artifact,
    )
    package = module().activation_package(
        store,
        manifest(),
        fit=fit,
        now=now,
        config_hash="a" * 64,
        owner="verified-operator",
        rollback_model_id="b" * 64,
    )
    assert package.model_id == fit.candidate_id
    assert package.grants_execution is False
    assert store.state(manifest().experiment_id).state == "CANDIDATE_SHADOW"
    assert package.risk == manifest().risk
    assert package.roster == manifest().roster
    assert package.training_source_hash == module().content_id(request().training)
    assert "SEEN_VALIDATION_IS_NOT_FORWARD_PROFITABILITY" in package.limitations
    assert store.state(manifest().experiment_id).activation_id is None
    changed = package.model_copy(update={"model_id": "c" * 64})
    assert changed.package_id != package.package_id


def test_activation_package_cannot_use_fabricated_fit_receipt(context):
    store, _ = context
    now = manifest().registered_at
    invented = module().FitReceipt(
        experiment_id=manifest().experiment_id,
        recorded_at=now,
        request_id="a" * 64,
        trial_id="invented",
        status="SUCCEEDED",
        charged=True,
        candidate_id="b" * 64,
        validation_mse=0.0,
        reason="invented",
        synthetic=True,
    )
    with pytest.raises(ValueError, match="AUTHORITATIVE_FIT"):
        module().activation_package(
            store,
            manifest(),
            fit=invented,
            now=now,
            config_hash="c" * 64,
            owner="verified-operator",
            rollback_model_id=None,
        )


def test_real_canonical_charge_is_committed_before_synthetic_engine(context, tmp_path):
    from quantbot.learner.forward import _REGISTRY, _registry_session
    from quantbot.research.intraday_accounting import verified_intraday_attempt_ids
    from quantbot.storage import Database

    store, _ = context
    registry_path = tmp_path / "authoritative-research.db"
    database = Database.create(registry_path)
    database.close()
    token = _REGISTRY.set(registry_path)
    try:
        prepared = request()
        charger = module().CanonicalTrialCharger(prepared, clock=lambda: manifest().registered_at)

        def engine(row):
            with _registry_session() as session:
                assert row.trial_id in verified_intraday_attempt_ids(session)
            return fake_artifact(row)

        result = module().run_registered_fit(
            store,
            manifest(),
            prepared,
            guard=Guard(),
            charger=charger,
            now=manifest().registered_at,
            synthetic_engine=engine,
        )
        assert result.status == "SUCCEEDED"
        assert charger.used_calls(manifest().experiment_id) == 1
    finally:
        _REGISTRY.reset(token)


def test_request_queue_never_fits_and_initial_pair_is_frozen(context):
    store, (training, validation) = context
    queued = module().schedule_initial_fits(
        store,
        manifest(),
        training=training,
        validation=validation,
        now=manifest().registered_at,
        guard=Guard(),
    )
    assert tuple(row.family for row in queued) == ("constant", "ridge")
    assert not any(
        e.kind in {"FitStart", "CandidateArtifact"}
        for e in store.read_as_of(manifest().experiment_id, manifest().registered_at)
    )
    pending = module().research_eligibility(store, manifest(), now=manifest().registered_at)
    assert pending.request_id == queued[0].request_id
    result = module().run_registered_fit(
        store,
        manifest(),
        pending,
        guard=Guard(),
        charger=Charger(),
        now=manifest().registered_at,
        synthetic_engine=fake_artifact,
    )
    assert result.status == "SUCCEEDED"
    next_request = module().research_eligibility(store, manifest(), now=manifest().registered_at)
    assert next_request.request_id == queued[1].request_id
    again = module().schedule_initial_fits(
        store,
        manifest(),
        training=training,
        validation=validation,
        now=manifest().registered_at + timedelta(minutes=1),
        guard=Guard(),
    )
    assert again == queued


def test_guard_runs_before_opening_research_input_files(tmp_path):
    missing = tmp_path / "must-not-open.json"
    with pytest.raises(ValueError, match="PROTECTED"):
        module().load_initial_inputs(
            manifest(),
            training_path=missing,
            validation_path=missing,
            training_hash="a" * 64,
            validation_hash="b" * 64,
            guard=Guard(False),
        )


def test_historical_retrieval_time_is_preserved_not_backdated_to_validation():
    training, validation = inputs()
    retrieved = manifest().registered_at - timedelta(seconds=1)

    def archived(rows):
        return tuple(
            row.model_copy(
                update={
                    "features": row.features.model_copy(
                        update={
                            "receipt_at": retrieved,
                            "evaluation_class": "EXPLORATORY_HISTORICAL",
                        }
                    ),
                    "available_at": retrieved,
                    "evaluation_class": "EXPLORATORY_HISTORICAL",
                }
            )
            for row in rows
        )

    prepared = module().prepare_fit_request(
        manifest(),
        family="ridge",
        training=archived(training),
        validation=archived(validation),
        now=manifest().registered_at,
        guard=Guard(),
    )
    assert all(row.available_at == retrieved for row in prepared.training)
    assert prepared.training_sessions == 20 and prepared.validation_sessions == 5
