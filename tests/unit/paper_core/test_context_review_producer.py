from datetime import datetime, timedelta

import pytest
from tests.unit.paper_core.test_external_context import NOW


def test_prompt_and_strict_response_never_accept_invented_evidence():
    from quantbot.paper_core.context_review_producer import build_prompt, parse_review

    prompt = build_prompt("AAPL", "Apple raises revenue outlook.")
    assert "-1 means negative" in prompt and "+1 means positive" in prompt
    assert "outcome" not in prompt.lower()
    good = '{"polarity":1,"confidence":0.9,"uncertain":false,"quote":"raises revenue outlook"}'
    assert parse_review(good, "Apple raises revenue outlook.").polarity == 1
    with pytest.raises(ValueError, match="EXACT_SOURCE_QUOTE"):
        parse_review(good, "unrelated article")
    with pytest.raises(ValueError):
        parse_review(good[:-1] + ',"trade":"BUY"}', "Apple raises revenue outlook.")


def test_uncertainty_is_inconclusive_and_generated_authority_is_explicit():
    from quantbot.paper_core.context_review_producer import TeacherResponse, generated_review

    result = TeacherResponse(polarity=0, confidence=0.9, uncertain=True, quote="mixed")
    review = generated_review(
        result,
        experiment_id="synthetic",
        reading_id="a" * 64,
        policy_id="b" * 64,
        evidence_id="c" * 64,
        now=NOW,
    )
    assert review.verdict == "INCONCLUSIVE" and review.corrected_polarity is None
    assert review.label_authority == "MODEL_GENERATED" and review.outcome_net_bps is None


def test_hourly_reservation_survives_failure_and_rejects_backwards_clock(tmp_path):
    from quantbot.paper_core.context_review_producer import reserve_attempt
    from quantbot.paper_core.discovery import DiscoveryStore

    research = DiscoveryStore(tmp_path / "synthetic.db")
    assert reserve_attempt(research, policy_id="a" * 64, now=NOW)
    assert not reserve_attempt(research, policy_id="b" * 64, now=NOW + timedelta(minutes=59))
    assert not reserve_attempt(research, policy_id="a" * 64, now=NOW - timedelta(hours=1))
    assert reserve_attempt(research, policy_id="a" * 64, now=NOW + timedelta(hours=1))


def test_retained_source_producer_teacher_isolation_and_single_attempt(tmp_path, monkeypatch):
    from tests.unit.learner.test_context_admission import setup_plan
    from tests.unit.learner.test_context_integration import news
    from tests.unit.paper_core.test_external_context import permissions

    from quantbot.learner.context_admission import (
        ContextEvaluationPlan,
        ContextReadingScope,
        adopt_context_plan,
    )
    from quantbot.learner.context_feedback import ReadingReview
    from quantbot.learner.context_integration import (
        evaluate_retained_context,
        journal_context_snapshot,
    )
    from quantbot.paper_core import context_review_producer as producer
    from quantbot.paper_core.context_integration import capture_research_context
    from quantbot.paper_core.context_learning import BINDING_KEY, ContextLearningBinding
    from quantbot.research.manifest import content_id

    research, experiments, registry, old = setup_plan(tmp_path)
    rights = {"alpaca-news": permissions()}
    policy = producer.ContextReviewProducerPolicy(
        recorded_at=old.recorded_at,
        experiment_id=old.experiment_id,
        source_policy_id=content_id(rights),
    )
    plan = ContextEvaluationPlan.model_validate(
        old.model_dump()
        | {
            "teacher_policy_id": policy.policy_id,
            "target": "MODEL_TEACHER_AGREEMENT_V1",
            "reading_scope": ContextReadingScope(
                source_policy_id=content_id(rights), target="MODEL_TEACHER_AGREEMENT_V1"
            ),
        }
    )
    adopt_context_plan(
        experiments,
        plan,
        supersedes_plan_id=old.plan_id,
        registry_path=registry,
        declaration_path=registry,
        now=old.recorded_at,
    )
    binding = ContextLearningBinding(
        experiment_id=old.experiment_id,
        core_store_identity="synthetic-not-opened",
        experiment_store_path=experiments.path,
        registry_path=registry,
        declaration_path=registry,
        manifest_hash=plan.manifest_hash,
        plan_id=plan.plan_id,
        recorded_at=old.recorded_at,
        capture_rowid=0,
        decision_rowid=0,
        outcome_rowid=0,
    )
    with research.connection() as db:
        research._set(db, "context:source-permissions", list(rights.values()))
        research._set(db, producer.POLICY_KEY, policy)
        research._set(db, BINDING_KEY, binding)
    news(research)
    snapshot = capture_research_context(research, symbol="AAPL", at=NOW, permissions=rights)
    journal_context_snapshot(
        experiments,
        research,
        experiment_id=old.experiment_id,
        snapshot_id=snapshot.snapshot_id,
        recorded_at=NOW,
    )
    calls = []

    def infer(_policy, prompt):
        calls.append(prompt)
        assert "AAPL beats earnings" in prompt and "outcome_net" not in prompt
        return {
            "response": '{"polarity":1,"confidence":0.9,"uncertain":false,'
            '"quote":"AAPL beats earnings"}'
        }

    monkeypatch.setattr(producer, "local_inference", infer)

    # Production uses actual completion time. This synthetic November 6 case
    # must not depend on the wall date when the suite happens to execute.
    class CompletionClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(seconds=10)

    monkeypatch.setattr(producer, "datetime", CompletionClock)
    assert producer.consider_review(research, now=NOW) == "MODEL_GENERATED_REVIEW_RETAINED"
    monkeypatch.setattr(
        experiments,
        "read_as_of",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("idle scan")),
    )
    assert (
        producer.consider_review(research, now=NOW + timedelta(minutes=1)) == "HOURLY_TEACHER_LIMIT"
    )
    monkeypatch.undo()
    events = experiments.read_as_of(
        old.experiment_id, NOW + timedelta(days=1), maximum_records=4000, kinds=("ReadingReview",)
    )
    label = events[0].decode(ReadingReview)
    assert len(calls) == 1 and label.label_authority == "MODEL_GENERATED"
    assert label.recorded_at == NOW + timedelta(seconds=10)
    result = evaluate_retained_context(
        experiments,
        research,
        experiment_id=old.experiment_id,
        split_at=NOW + timedelta(hours=1),
        as_of=NOW + timedelta(days=1),
    )
    assert result.status == "INSUFFICIENT_EVIDENCE" and result.eligible_examples == 0
    teacher = evaluate_retained_context(
        experiments,
        research,
        experiment_id=old.experiment_id,
        split_at=NOW + timedelta(hours=1),
        as_of=NOW + timedelta(days=1),
        teacher_policy_id=policy.policy_id,
    )
    assert teacher.status == "ADMISSION_REQUIRED"


def test_uncommissioned_producer_opens_no_experiment(tmp_path, monkeypatch):
    from quantbot.paper_core import context_review_producer as producer
    from quantbot.paper_core.discovery import DiscoveryStore

    monkeypatch.setattr(
        producer,
        "ExperimentStore",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("not commissioned")),
    )
    assert (
        producer.consider_review(DiscoveryStore(tmp_path / "synthetic.db"), now=NOW)
        == "UNCOMMISSIONED"
    )


def test_crash_temp_recovery_requires_marker_free_listener_and_lock(tmp_path, monkeypatch):
    from quantbot.paper_core import context_review_producer as producer
    from quantbot.storage import SingleWriterLock
    from quantbot.storage.lock import AlreadyLockedError

    root = tmp_path / "synthetic-private-runtime"
    root.mkdir()
    monkeypatch.setattr(producer, "_private_runtime", lambda policy: root)
    monkeypatch.setattr(producer, "_assert_listener_free", lambda: None)
    abandoned = producer._new_owned_temp(root)
    (abandoned / "scratch.bin").write_bytes(b"synthetic")
    with SingleWriterLock(root / "local-review.lock"):
        with pytest.raises(AlreadyLockedError):
            with producer._owned_runtime(None):
                pass
        assert abandoned.is_dir()
    with producer._owned_runtime(None):
        assert not abandoned.exists()
    unknown = root / "owned-attempt-unowned"
    unknown.mkdir()
    with pytest.raises(ValueError, match="OWNERSHIP"):
        with producer._owned_runtime(None):
            pass
    assert unknown.exists()


def test_recovery_refuses_nested_reparse_and_preserves_all_content(tmp_path, monkeypatch):
    from quantbot.paper_core import context_review_producer as producer

    root = tmp_path / "synthetic-private-runtime"
    root.mkdir()
    abandoned = producer._new_owned_temp(root)
    child = abandoned / "synthetic-junction"
    child.mkdir()
    original = producer._is_reparse
    monkeypatch.setattr(producer, "_is_reparse", lambda path: path == child or original(path))
    with pytest.raises(ValueError, match="REPARSE"):
        producer._retire_owned_temps(root)
    assert child.is_dir() and abandoned.is_dir()


def test_actual_windows_junction_is_not_followed_or_deleted(tmp_path):
    import os
    import subprocess

    from quantbot.paper_core import context_review_producer as producer

    if os.name != "nt":
        pytest.skip("Windows junction regression")
    root, outside = tmp_path / "private", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    protected = outside / "preserve.txt"
    protected.write_text("synthetic external content")
    abandoned = producer._new_owned_temp(root)
    junction = abandoned / "junction"
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference='Stop'; New-Item -ItemType Junction "
            "-Path $env:ELROND_TEST_LINK -Target $env:ELROND_TEST_TARGET | Out-Null",
        ],
        env=os.environ | {"ELROND_TEST_LINK": str(junction), "ELROND_TEST_TARGET": str(outside)},
        check=True,
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        with pytest.raises(ValueError, match="REPARSE"):
            producer._retire_owned_temps(root)
        assert protected.read_text() == "synthetic external content"
        assert abandoned.is_dir()
    finally:
        junction.rmdir()


@pytest.mark.parametrize(
    "elapsed,expected",
    [(0, "DECLARED_TRAINING_WINDOW_NOT_STARTED"), (3, "DECLARED_REVIEW_WINDOW_CLOSED")],
)
def test_future_or_closed_review_window_is_not_a_configuration_failure(
    tmp_path, monkeypatch, elapsed, expected
):
    from types import SimpleNamespace

    from tests.unit.paper_core.test_external_context import permissions

    from quantbot.paper_core import context_review_producer as producer
    from quantbot.paper_core.context_learning import BINDING_KEY, ContextLearningBinding
    from quantbot.paper_core.discovery import DiscoveryStore
    from quantbot.research.manifest import content_id

    rights = {"alpaca-news": permissions()}
    policy = producer.ContextReviewProducerPolicy(
        recorded_at=NOW - timedelta(days=1),
        experiment_id="synthetic",
        source_policy_id=content_id(rights),
    )
    binding = ContextLearningBinding(
        experiment_id="synthetic",
        core_store_identity="synthetic",
        experiment_store_path=tmp_path / "never-opened.db",
        registry_path=tmp_path / "never-opened-registry.db",
        declaration_path=tmp_path / "never-opened-registry.db",
        manifest_hash="a" * 64,
        plan_id="b" * 64,
        recorded_at=policy.recorded_at,
        capture_rowid=0,
        decision_rowid=0,
        outcome_rowid=0,
    )
    research = DiscoveryStore(tmp_path / "synthetic.db")
    with research.connection() as db:
        research._set(db, producer.POLICY_KEY, policy)
        research._set(db, BINDING_KEY, binding)
    plan = SimpleNamespace(
        teacher_policy_id=policy.policy_id,
        training_start=NOW + timedelta(hours=1),
        evaluation_end=NOW + timedelta(hours=2),
        reading_scope=SimpleNamespace(source_policy_id=policy.source_policy_id),
    )
    monkeypatch.setattr(producer, "ExperimentStore", lambda *a: object())
    monkeypatch.setattr(producer, "load_context_plan", lambda *a, **k: plan)
    monkeypatch.setattr(producer, "load_source_permissions", lambda *a: rights)
    monkeypatch.setattr(
        producer,
        "local_inference",
        lambda *a: (_ for _ in ()).throw(AssertionError("outside declared window")),
    )
    assert producer.consider_review(research, now=NOW + timedelta(hours=elapsed)) == expected
