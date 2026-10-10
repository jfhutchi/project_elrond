"""Read-only progress requires retained receipts, not elapsed time or model claims."""
from datetime import timedelta
from importlib import import_module

import pytest

from quantbot.learner.experiment_store import ExperimentStore
from tests.fixtures.adaptive_paper import manifest
from tests.unit.learner.test_exploration_feedback import journal


def readiness(store, frozen, now):
    reader = getattr(import_module("quantbot.console.experiments"), "read_evidence_readiness", None)
    assert callable(reader), "Retained coverage/adaptation readiness reporting is missing"
    return reader(store, frozen, now=now)


def test_missing_receipts_are_unknown_and_do_not_grant_readiness(tmp_path):
    frozen = manifest()
    store = ExperimentStore(tmp_path / "progress.db", create=True)
    store.append(frozen, idempotency_key="manifest")
    before = store.path.read_bytes()
    result = readiness(store, frozen, frozen.registered_at)
    assert all(row.status == "UNKNOWN" for row in result.coverage)
    assert result.adaptation is None
    assert result.grants_execution is False
    assert store.path.read_bytes() == before


def test_coverage_retains_gap_and_productive_times_without_faking_recovery(tmp_path):
    from quantbot.learner.experiment_contract import ExperimentManifest
    from quantbot.learner.experiment_observation import build_observation_batch
    from tests.unit.learner.test_paper_exploration import example

    store, _, _, now = journal(tmp_path)
    frozen, source, _ = example()
    batch = build_observation_batch(frozen, source, now=source.received_at)
    store.append(batch, idempotency_key="productive")
    gap = batch.model_copy(update={
        "recorded_at": now, "features": (),
        "coverage": tuple(row.model_copy(update={"status": "MISSING", "reason": "MISSING"})
                          for row in batch.coverage),
    })
    store.append(gap, idempotency_key="gap")
    future = gap.model_copy(update={"recorded_at": now + timedelta(minutes=1)})
    store.append(future, idempotency_key="future")
    before = store.path.read_bytes()
    result = readiness(store, ExperimentManifest.model_validate(frozen.model_dump()), now)
    assert all(row.status == "MISSING" and row.recorded_at == now for row in result.coverage)
    assert all(row.last_productive_at == batch.recorded_at for row in result.coverage)
    assert all(row.last_gap_at == now for row in result.coverage)
    assert all(row.source_hash for row in result.coverage)
    assert result.synthetic is True
    assert store.path.read_bytes() == before


def test_adaptation_wait_is_measured_not_inferred_from_elapsed_time(tmp_path):
    from quantbot.learner.experiment_adaptation import AdaptationReceipt

    frozen = manifest()
    store = ExperimentStore(tmp_path / "progress.db", create=True)
    store.append(frozen, idempotency_key="manifest")
    receipt = AdaptationReceipt(
        experiment_id=frozen.experiment_id, recorded_at=frozen.registered_at,
        status="NORMAL_WAIT", reason="FRESH_TRAINING_AND_UNSEEN_VALIDATION_REQUIRED",
        fresh_complete_sessions=3, synthetic=True,
    )
    store.append(receipt, idempotency_key="assessment")
    result = readiness(store, frozen, frozen.registered_at + timedelta(days=20))
    assert result.adaptation.fresh_complete_sessions == 3
    assert result.adaptation.required_fresh_sessions == 10
    assert result.adaptation.recorded_at == receipt.recorded_at
    assert result.adaptation.fit_status is None
    assert result.adaptation.improvement_established is False


def test_queued_successor_requires_exact_registered_request(tmp_path):
    from quantbot.learner.experiment_adaptation import AdaptationReceipt

    frozen = manifest()
    store = ExperimentStore(tmp_path / "progress.db", create=True)
    store.append(frozen, idempotency_key="manifest")
    store.append(AdaptationReceipt(
        experiment_id=frozen.experiment_id, recorded_at=frozen.registered_at,
        status="QUEUED", reason="FIXED_FRESH_DATA_SUCCESSOR_QUEUED", request_id="a" * 64,
        parent_model_id="b" * 64, synthetic=True,
    ), idempotency_key="unproven-queue")
    with pytest.raises(ValueError, match="SUCCESSOR_REQUEST_UNVERIFIED"):
        readiness(store, frozen, frozen.registered_at)


@pytest.mark.parametrize("phase", ["NOT_STARTED", "STARTED", "SUCCEEDED", "FAILED"])
def test_registered_successor_progress_does_not_claim_adoption(tmp_path, phase):
    from quantbot.learner.experiment_adaptation import schedule_next_successor
    from quantbot.learner.experiment_research import (
        FitStart,
        research_eligibility,
        run_registered_fit,
    )
    from quantbot.research.manifest import content_id
    from tests.unit.learner.test_experiment_adaptation import context, forward_labels
    from tests.unit.learner.test_experiment_research import Guard, fake_artifact

    store, charger, parent = context.__wrapped__(tmp_path)
    labels = forward_labels(store, 10)
    now = labels[-1].available_at
    assessment = schedule_next_successor(store, manifest(), now=now, guard=Guard())
    store.append(assessment, idempotency_key="adaptation")
    request = research_eligibility(store, manifest(), now=now)
    if phase == "STARTED":
        store.append(FitStart(
            experiment_id=request.experiment_id, recorded_at=now, request_id=request.request_id,
            trial_id=request.trial_id, manifest_hash=content_id(manifest()), synthetic=True,
        ), idempotency_key="started")
    elif phase in {"SUCCEEDED", "FAILED"}:
        def failure(*args):
            raise ValueError("SYNTHETIC_ENGINE_FAILURE")

        run_registered_fit(store, manifest(), request, now=now, guard=Guard(), charger=charger,
                           synthetic_engine=fake_artifact if phase == "SUCCEEDED" else failure)
    before = store.path.read_bytes()
    result = readiness(store, manifest(), now)
    assert result.adaptation.fit_status == phase
    assert result.adaptation.request_id == request.request_id
    assert result.adaptation.parent_model_id == parent.candidate_id
    assert bool(result.adaptation.candidate_model_id) == (phase == "SUCCEEDED")
    assert result.adaptation.improvement_established is False
    assert store.state(manifest().experiment_id).activation_id is None
    assert store.path.read_bytes() == before


def test_coverage_cannot_claim_productivity_without_matching_features(tmp_path):
    from quantbot.learner.experiment_observation import build_observation_batch
    from tests.unit.learner.test_paper_exploration import example

    frozen, source, _ = example()
    store = ExperimentStore(tmp_path / "progress.db", create=True)
    store.append(frozen, idempotency_key="manifest")
    batch = build_observation_batch(frozen, source, now=source.received_at)
    store.append(batch.model_copy(update={"features": ()}), idempotency_key="unsupported")
    with pytest.raises(ValueError, match="OBSERVATION_LINEAGE_UNVERIFIED"):
        readiness(store, frozen, batch.recorded_at)


def test_readiness_html_and_json_share_retained_receipts(tmp_path):
    from fastapi.testclient import TestClient

    from quantbot.console.app import create_app
    from quantbot.learner.experiment_adaptation import AdaptationReceipt
    from quantbot.learner.experiment_observation import build_observation_batch
    from quantbot.storage import Database
    from tests.unit.learner.test_paper_exploration import example

    store, _, _, now = journal(tmp_path)
    frozen, source, _ = example()
    batch = build_observation_batch(frozen, source, now=source.received_at)
    store.append(batch, idempotency_key="coverage")
    store.append(AdaptationReceipt(
        experiment_id=frozen.experiment_id, recorded_at=now, status="NORMAL_WAIT",
        reason="FRESH_TRAINING_AND_UNSEEN_VALIDATION_REQUIRED", fresh_complete_sessions=3,
        synthetic=True,
    ), idempotency_key="assessment")
    before = store.path.read_bytes()
    with Database.create(tmp_path / "console.db") as database:
        client = TestClient(create_app(database, experiment_store=store.path))
        html = client.get("/experiments/" + frozen.experiment_id)
        assert html.status_code == 200
        assert "Measured fresh complete sessions: 3 / 10" in html.text
        assert "Queued, fitted and evidence-supported adoption are separate states" in html.text
        assert batch.coverage[0].source_hash in html.text
        api = client.get("/api/experiments/" + frozen.experiment_id).json()
        assert api["detail"]["evidence_readiness"]["adaptation"]["fresh_complete_sessions"] == 3
        assert api["detail"]["evidence_readiness"]["grants_execution"] is False
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("future", [False, True])
def test_historical_features_do_not_claim_forward_coverage(tmp_path, future):
    from quantbot.learner.experiment_observation import build_observation_batch
    from tests.unit.learner.test_paper_exploration import example

    frozen, source, _ = example()
    store = ExperimentStore(tmp_path / "historical.db", create=True)
    store.append(frozen, idempotency_key="manifest")
    batch = build_observation_batch(frozen, source, now=source.received_at)
    features = tuple(row.model_copy(update={
        "evaluation_class": "EXPLORATORY_HISTORICAL",
        "receipt_at": batch.recorded_at + timedelta(minutes=1) if future else row.receipt_at,
    }) for row in batch.features)
    store.append(batch.model_copy(update={"features": features}), idempotency_key="historical")
    with pytest.raises(ValueError, match="OBSERVATION_LINEAGE_UNVERIFIED"):
        readiness(store, frozen, batch.recorded_at)


@pytest.mark.parametrize("fault", ["uncharged", "unknown_charge", "candidate_before_start"])
def test_success_requires_charged_chronological_fit_lineage(tmp_path, fault):
    from quantbot.learner.experiment_adaptation import schedule_next_successor
    from quantbot.learner.experiment_contract import CandidateArtifact
    from quantbot.learner.experiment_research import (
        FitReceipt,
        FitStart,
        research_eligibility,
        run_registered_fit,
    )
    from tests.unit.learner.test_experiment_adaptation import context, forward_labels
    from tests.unit.learner.test_experiment_research import Guard, fake_artifact

    original, charger, _ = context.__wrapped__(tmp_path)
    now = forward_labels(original, 10)[-1].available_at
    assessment = schedule_next_successor(original, manifest(), now=now, guard=Guard())
    request = research_eligibility(original, manifest(), now=now)
    run_registered_fit(original, manifest(), request, now=now, guard=Guard(), charger=charger,
                       synthetic_engine=fake_artifact)
    events = original.read_as_of(request.experiment_id, now)
    start = next(row.decode(FitStart) for row in events if row.kind == "FitStart"
                 and row.decode(FitStart).request_id == request.request_id)
    fit = next(row.decode(FitReceipt) for row in events if row.kind == "FitReceipt"
               and row.decode(FitReceipt).request_id == request.request_id)
    candidate = next(row.decode(CandidateArtifact) for row in events
                     if row.kind == "CandidateArtifact"
                     and row.decode(CandidateArtifact).model_id == fit.candidate_id)
    if fault == "candidate_before_start":
        start = start.model_copy(update={"recorded_at": now + timedelta(seconds=1)})
        fit = fit.model_copy(update={"recorded_at": now + timedelta(seconds=2)})
    else:
        fit = fit.model_copy(update={"charged": False if fault == "uncharged" else None})
    altered = ExperimentStore(tmp_path / "altered.db", create=True)
    for i, row in enumerate((manifest(), request, assessment, start, candidate, fit)):
        altered.append(row, idempotency_key=str(i))
    with pytest.raises(ValueError, match="SUCCESSOR_(FIT|CANDIDATE)_UNVERIFIED"):
        readiness(altered, manifest(), now + timedelta(seconds=3))
