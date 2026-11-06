"""Durable synthetic context/review flow uses existing stores, never runtime data."""

from datetime import timedelta

import pytest

from quantbot.paper_core.discovery import DiscoveryStore
from quantbot.paper_core.discovery_research import _initialize
from quantbot.research.manifest import canonical_result_json, content_id
from tests.unit.learner.test_context_feedback import review
from tests.unit.learner.test_experiment_contract import Guard, manifest_data
from tests.unit.paper_core.test_external_context import NOW, permissions


def news(state, index=0, *, late=False):
    at = NOW + timedelta(days=index)
    payload = dict(
        kind="NEWS_RESEARCH_V1",
        source_id=f"alpaca-news:{index}",
        uri=f"https://synthetic.invalid/{index}",
        publisher="synthetic",
        title="AAPL beats earnings",
        summary="",
        symbols=["AAPL"],
        published_at=(at - timedelta(minutes=5)).isoformat(),
        updated_at=(at - timedelta(minutes=5)).isoformat(),
        retrieved_at=(at + timedelta(seconds=1) if late else at - timedelta(minutes=4)).isoformat(),
        parser="synthetic-v1",
        parsed_item_sha256="a" * 64,
    )
    identity = content_id(payload)
    with state.connection() as db:
        db.execute(
            "insert into research_documents values(?,?)", (identity, canonical_result_json(payload))
        )
        db.execute(
            "insert into research_symbols values(?,?,?,?,?,?)",
            (
                "AAPL",
                identity,
                payload["source_id"],
                payload["retrieved_at"],
                payload["published_at"],
                payload["kind"],
            ),
        )
    return identity


def stores(tmp_path):
    from quantbot.learner.experiment_contract import ExperimentManifest
    from quantbot.learner.experiment_store import ExperimentStore

    research = DiscoveryStore(tmp_path / "synthetic-research.db")
    _initialize(research)
    experiments = ExperimentStore(tmp_path / "synthetic-experiments.db", create=True)
    manifest = ExperimentManifest(**(manifest_data() | {"experiment_id": "synthetic-context"}))
    experiments.append(manifest, idempotency_key="manifest")
    return research, experiments


class Charger:
    def __init__(self):
        self.calls = []

    def used_calls(self, experiment_id):
        return len(self.calls)

    def charge(self, trial_id, manifest_hash):
        self.calls.append((trial_id, manifest_hash))


def test_capture_is_durable_observed_only_and_first_seen_causal(tmp_path):
    from quantbot.paper_core.context_integration import (
        capture_research_context,
        load_context_capture,
    )

    research, _ = stores(tmp_path)
    identity = news(research)
    news(research, 1, late=True)
    snapshot = capture_research_context(
        research, symbol="AAPL", at=NOW, permissions={"alpaca-news": permissions()}
    )
    assert snapshot is not None
    capture = load_context_capture(research, snapshot.snapshot_id)
    assert capture.snapshot == snapshot
    assert snapshot.observed_document_ids == (identity,)
    assert snapshot.used_document_ids == ()
    assert dict(snapshot.features)["document_count"] == 0
    assert capture.bindings[0].first_seen_at == NOW - timedelta(minutes=4)
    assert capture.bindings[0].published_at == NOW - timedelta(minutes=5)
    assert "AAPL beats earnings" not in capture.model_dump_json()
    repeated = capture_research_context(
        research,
        symbol="AAPL",
        at=NOW + timedelta(seconds=10),
        permissions={"alpaca-news": permissions()},
    )
    assert repeated == snapshot
    with research.connection() as db:
        assert db.execute("select count(*) from receipts").fetchone()[0] == 1


def test_unverified_rights_never_retain_or_process_content(tmp_path):
    from quantbot.paper_core.context_integration import (
        capture_research_context,
        permission_coverage,
    )

    research, _ = stores(tmp_path)
    news(research)
    snapshot = capture_research_context(research, symbol="AAPL", at=NOW, permissions={})
    assert snapshot is None
    with research.connection() as db:
        assert db.execute("select count(*) from receipts").fetchone()[0] == 0
    coverage = {row.provider: row for row in permission_coverage({}, at=NOW)}
    assert coverage["alpaca-news"].status == "UNKNOWN"
    assert coverage["x"].status == coverage["reddit"].status == "UNAVAILABLE"


def test_retained_reviews_reach_charged_persistent_fit_and_invalidation(tmp_path):
    from quantbot.learner.context_integration import (
        evaluate_retained_context,
        journal_context_snapshot,
        journal_invalidation,
        load_retained_candidate,
    )
    from quantbot.paper_core.context_integration import capture_research_context

    research, experiments = stores(tmp_path)
    first_id = None
    for index in range(16):
        news(research, index)
        at = NOW + timedelta(days=index)
        snapshot = capture_research_context(
            research,
            symbol="AAPL",
            at=at,
            permissions={"alpaca-news": permissions()},
            maximum_documents=1,
        )
        assert snapshot is not None
        first_id = first_id or snapshot.observed_document_ids[0]
        journal_context_snapshot(
            experiments,
            research,
            experiment_id="synthetic-context",
            snapshot_id=snapshot.snapshot_id,
            recorded_at=at,
        )
        experiments.append(review(snapshot.readings[0]), idempotency_key=f"review:{index}")
    charger = Charger()
    result = evaluate_retained_context(
        experiments,
        research,
        experiment_id="synthetic-context",
        split_at=NOW + timedelta(days=10),
        as_of=NOW + timedelta(days=20),
        guard=Guard(),
        charger=charger,
    )
    assert result.status == "EVALUATED" and result.candidate_id
    assert len(charger.calls) == 1
    candidate = load_retained_candidate(
        experiments,
        experiment_id="synthetic-context",
        candidate_id=result.candidate_id,
        at=NOW + timedelta(days=20),
    )
    assert candidate.heldout_mse < candidate.baseline_heldout_mse
    assert candidate.grants_activation is False
    again = evaluate_retained_context(
        experiments,
        research,
        experiment_id="synthetic-context",
        split_at=NOW + timedelta(days=10),
        as_of=NOW + timedelta(days=20),
        guard=Guard(),
        charger=charger,
    )
    assert again == result and len(charger.calls) == 1
    journal_invalidation(
        experiments,
        experiment_id="synthetic-context",
        reference_id=first_id,
        at=NOW + timedelta(days=21),
        reason="REDACTED",
    )
    with pytest.raises(ValueError, match="INVALIDATED"):
        load_retained_candidate(
            experiments,
            experiment_id="synthetic-context",
            candidate_id=result.candidate_id,
            at=NOW + timedelta(days=22),
        )


def test_no_retained_reviews_or_admission_produces_durable_refusal(tmp_path):
    from quantbot.learner.context_integration import evaluate_retained_context

    research, experiments = stores(tmp_path)
    result = evaluate_retained_context(
        experiments,
        research,
        experiment_id="synthetic-context",
        split_at=NOW + timedelta(days=10),
        as_of=NOW + timedelta(days=20),
    )
    assert result.status == "INSUFFICIENT_EVIDENCE" and result.candidate_id is None
    events = experiments.read_as_of("synthetic-context", NOW + timedelta(days=20))
    assert any(event.kind == "ContextEvaluation" for event in events)


def test_context_link_preserves_legacy_identity_and_price_only_decision(tmp_path):
    from quantbot.paper_core.contracts import Observation
    from quantbot.paper_core.trader import evaluate_entry
    from tests.fixtures.core_m2 import prepare

    db, store, _, proof, observation, model = prepare(tmp_path)
    try:
        legacy = observation.model_dump()
        assert "context_snapshot_id" not in legacy
        assert content_id(Observation.model_validate(legacy)) == content_id(observation)
        original = evaluate_entry(store.config, observation, model, proof)
        linked = Observation.model_validate(legacy | {"context_snapshot_id": "a" * 64})
        decision = evaluate_entry(store.config, linked, model, proof)
        assert content_id(linked) != content_id(observation)
        assert decision.context_snapshot_id == "a" * 64
        assert (decision.action, decision.prediction_bps, decision.gates) == (
            original.action,
            original.prediction_bps,
            original.gates,
        )
        assert "context_snapshot_id" not in original.model_dump()
    finally:
        db.close()


def test_sec_metadata_keeps_unknown_publication_and_distinct_snapshot_identity(tmp_path):
    from quantbot.paper_core.context_integration import (
        capture_research_context,
        load_context_capture,
    )

    research, _ = stores(tmp_path)
    payload = dict(
        kind="SEC_COMPANY_RESEARCH_V1",
        source_id="sec-company:synthetic",
        symbols=["AAPL"],
        published_at=None,
        updated_at=None,
        retrieved_at=(NOW - timedelta(minutes=1)).isoformat(),
    )
    identity = content_id(payload)
    with research.connection() as db:
        db.execute(
            "insert into research_documents values(?,?)", (identity, canonical_result_json(payload))
        )
        db.execute(
            "insert into research_symbols values(?,?,?,?,?,?)",
            (
                "AAPL",
                identity,
                payload["source_id"],
                payload["retrieved_at"],
                payload["retrieved_at"],
                payload["kind"],
            ),
        )
    snapshot = capture_research_context(
        research,
        symbol="AAPL",
        at=NOW,
        permissions={
            "sec-edgar": permissions(
                provider="sec-edgar", retention="UNKNOWN", processing="UNKNOWN", training="UNKNOWN"
            )
        },
    )
    assert snapshot is not None
    capture = load_context_capture(research, snapshot.snapshot_id)
    assert snapshot.observed_document_ids == (identity,)
    assert not snapshot.readings and not snapshot.used_document_ids
    assert capture.bindings[0].published_at is None
    assert capture.bindings[0].first_seen_at == NOW - timedelta(minutes=1)


def test_missing_admission_never_opens_source_payload_for_training(tmp_path, monkeypatch):
    from quantbot.learner.context_integration import (
        evaluate_retained_context,
        journal_context_snapshot,
    )
    from quantbot.paper_core.context_integration import capture_research_context

    research, experiments = stores(tmp_path)
    news(research)
    snapshot = capture_research_context(
        research, symbol="AAPL", at=NOW, permissions={"alpaca-news": permissions()}
    )
    assert snapshot is not None
    journal_context_snapshot(
        experiments,
        research,
        experiment_id="synthetic-context",
        snapshot_id=snapshot.snapshot_id,
        recorded_at=NOW,
    )
    experiments.append(review(snapshot.readings[0]), idempotency_key="review")

    def forbidden(*args):
        pytest.fail("no canonical admission may open source training content")

    monkeypatch.setattr(
        "quantbot.learner.context_integration.retained_context_documents", forbidden
    )
    result = evaluate_retained_context(
        experiments,
        research,
        experiment_id="synthetic-context",
        split_at=NOW + timedelta(days=10),
        as_of=NOW + timedelta(days=20),
    )
    assert result.status == "ADMISSION_REQUIRED"


def test_experiment_read_bound_fails_instead_of_silent_sample_truncation(tmp_path):
    from quantbot.learner.experiment_store import ConsentNotice

    _, experiments = stores(tmp_path)
    experiments.append(
        ConsentNotice(experiment_id="synthetic-context", consent_id="synthetic", recorded_at=NOW),
        idempotency_key="consent",
    )
    with pytest.raises(ValueError, match="BOUND_EXCEEDED"):
        experiments.read_as_of("synthetic-context", NOW, maximum_records=1)


def test_permission_records_are_loaded_from_existing_metadata_only(tmp_path):
    from quantbot.paper_core.context_integration import load_source_permissions

    research, _ = stores(tmp_path)
    assert load_source_permissions(research) == {}
    permission = permissions(training="UNKNOWN")
    with research.connection() as db:
        research._set(db, "context:source-permissions", [permission.model_dump(mode="json")])
    assert load_source_permissions(research) == {"alpaca-news": permission}
    with research.connection() as db:
        research._set(db, "context:source-permissions", [{"provider": "alpaca-news"}])
    with pytest.raises(ValueError):
        load_source_permissions(research)
