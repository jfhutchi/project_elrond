"""Fixed learning envelope and before-data guard boundaries."""

import pytest


def test_shadow_predictive_artifact_cannot_authorize_entry(tmp_path):
    from tests.fixtures.core_m2 import prepare

    from quantbot.paper_core.contracts import ModelArtifact
    from quantbot.paper_core.trader import evaluate_entry

    db, store, broker, proof, observation, _ = prepare(tmp_path)
    try:
        observation = observation.model_copy(
            update={"feature_version": "five-minute-price-alpha-v1"}
        )
        decision = evaluate_entry(
            store.config, observation, ModelArtifact.fixture_constant(-20), proof
        )
        assert "SHADOW_MODEL_NOT_ACTIVATED" in decision.gates
        assert decision.action == "WAIT" and not broker.posts
    finally:
        db.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("alpha", 2),
        ("seed", 1),
        ("clip_bps", 101),
        ("fit_timeout_seconds", 31),
        ("max_sessions", 61),
        ("min_training", 29),
        ("min_training_sessions", 4),
        ("min_validation", 9),
        ("min_validation_sessions", 4),
        ("embargo_minutes", 29),
        ("min_improvement", 0.04),
        ("fits_per_session", 2),
    ],
)
def test_envelope_is_fixed(field, value):
    from pydantic import ValidationError

    from quantbot.paper_core.contracts import LearningPolicy

    with pytest.raises(ValidationError):
        LearningPolicy(**{field: value})


@pytest.mark.parametrize(
    "kind",
    ["protected", "forward", "declared", "unavailable", "costs", "lane", "policy", "features"],
)
def test_canary_is_excluded_before_any_materialization(tmp_path, monkeypatch, kind):
    import json
    from datetime import UTC, datetime, timedelta

    from tests.fixtures.core_m2 import prepare
    from tests.unit.research.test_registry import make_draft, register, window

    from quantbot.paper_core import learning
    from quantbot.paper_core.contracts import LearningPolicy
    from quantbot.paper_core.store import table
    from quantbot.research.registry import DataRole, HypothesisRegistry
    from quantbot.storage.schema import learner_datasets

    db, store, _, _, _, _ = prepare(tmp_path)
    cutoff = datetime(2026, 10, 15, tzinfo=UTC)
    try:
        with db.transaction() as session:
            obs = {
                "feature_version": "bad" if kind == "features" else "five-minute-price-alpha-v1",
                "session_close": "2026-10-14T20:00:00Z",
                "features": "DO_NOT_HYDRATE",
            }
            session.execute(
                table("core_observations").insert().values(id="canary-obs", payload=json.dumps(obs))
            )
            session.execute(
                table("core_decisions")
                .insert()
                .values(id="canary-lot", payload='{"observation_id":"canary-obs"}')
            )
            session.execute(
                table("core_outcome_revisions")
                .insert()
                .values(
                    id="canary-target",
                    payload=json.dumps(
                        {
                            "lot_id": "canary-lot",
                            "revision": 1,
                            "source_start": "2026-10-14T14:00:00Z",
                            "source_end": "2026-10-14T15:00:00Z",
                            "available_at": "2026-10-16T00:00:00Z"
                            if kind == "unavailable"
                            else "2026-10-14T16:00:00Z",
                            "dataset": "EXPLICIT_SYNTHETIC",
                            "policy_id": "foreign" if kind == "policy" else store.config.policy_id,
                            "evidence_lane": "BROKER_PAPER" if kind == "lane" else "SYNTHETIC_TEST",
                            "status": "COSTS_PENDING" if kind == "costs" else "COMPLETE",
                            "net_pnl": "DO_NOT_HYDRATE",
                        }
                    ),
                )
            )
            session.execute(
                table("core_outcome_index")
                .insert()
                .values(
                    lot_id="canary-lot",
                    outcome_id="canary-target",
                    available_at="2026-10-16T00:00:00Z"
                    if kind == "unavailable"
                    else "2026-10-14T16:00:00Z",
                    source_start="2026-10-14T14:00:00Z",
                    source_end="2026-10-14T15:00:00Z",
                    dataset="EXPLICIT_SYNTHETIC",
                    policy_id="foreign" if kind == "policy" else store.config.policy_id,
                    evidence_lane="BROKER_PAPER" if kind == "lane" else "SYNTHETIC_TEST",
                    status="COSTS_PENDING" if kind == "costs" else "COMPLETE",
                )
            )
            if kind in {"protected", "forward"}:
                role = (
                    DataRole.PROTECTED_EVALUATION if kind == "protected" else DataRole.FORWARD_PAPER
                )
                register(
                    HypothesisRegistry(session),
                    make_draft(
                        windows=(
                            window(role, "2026-10-14", "2026-10-14", dataset="EXPLICIT_SYNTHETIC"),
                        )
                    ),
                    now=cutoff - timedelta(days=1),
                )
            if kind == "declared":
                session.execute(
                    learner_datasets.insert().values(
                        dataset_id="canary-declaration",
                        document_hash="d" * 64,
                        document_json=json.dumps(
                            {
                                "holdout_window": {
                                    "dataset": "EXPLICIT_SYNTHETIC",
                                    "start": "2026-10-14",
                                    "end": "2026-10-14",
                                },
                                "rows": "DO_NOT_HYDRATE_DATASET",
                            }
                        ),
                    )
                )

        def forbidden(*args, **kwargs):
            raise AssertionError(
                "protected/unavailable/incompatible canary reached materialization"
            )

        monkeypatch.setattr(learning, "_materialize", forbidden)
        monkeypatch.setattr(learning, "_fit_subprocess", forbidden)
        from tests.fixtures.core_m4 import ExecutionClock

        receipt = learning.consider_learning(
            store, LearningPolicy(), cutoff=cutoff, execution_clock=ExecutionClock(cutoff)
        )
        assert receipt.status == "NO_CHANGE" and receipt.considered_ids == ("canary-target",)
        assert len(receipt.exclusions) == 1
        assert receipt.trial_charge_id is None
    finally:
        db.close()


@pytest.mark.parametrize(
    "dataset,overlap",
    [
        ("sip-us-equities-daily", True),
        ("alpaca-us-equities-daily", True),
        ("us-equities-daily", True),
        ("independent", False),
    ],
)
def test_permanent_failed_learner_consumption_blocks_untouched_claims_across_aliases(
    tmp_path, dataset, overlap
):
    from datetime import date

    from tests.unit.research.test_learner_burden import record_learner_attempt
    from tests.unit.research.test_registry import make_draft, register, window

    from quantbot.research.registry import (
        DataRole,
        HypothesisRegistry,
        RefusalReason,
        RegistrationRefused,
    )
    from quantbot.storage import Database

    with Database.create(tmp_path / "claims.db") as db:
        with db.transaction() as session:
            record_learner_attempt(session, "failed-before-fit")
        with db.transaction() as session:
            registry = HypothesisRegistry(session)
            candidate = make_draft(
                windows=(
                    window(
                        DataRole.PROTECTED_EVALUATION, "2024-01-01", "2024-12-31", dataset=dataset
                    ),
                )
            )
            if overlap:
                with pytest.raises(RegistrationRefused) as refusal:
                    register(registry, candidate)
                assert refusal.value.reason is RefusalReason.CONTAMINATED_WINDOW
                assert "permanently inspected learner" in refusal.value.detail
            else:
                assert register(registry, candidate)
        with db.transaction() as session:
            registry = HypothesisRegistry(session)
            assert registry.learner_trials_spent() == 1
            later = make_draft(
                hypothesis_id="H-NONOVERLAP",
                materially_different="independent later observation window",
                windows=(
                    window(
                        DataRole.PROTECTED_EVALUATION,
                        "2026-01-01",
                        "2026-07-31",
                        dataset="alpaca-us-equities-daily",
                    ),
                ),
            )
            assert register(registry, later)
            from quantbot.paper_core.learning import _guard

            assert _guard(
                session,
                "sip-us-equities-daily",
                date(2026, 3, 1),
                date(2026, 3, 2),
                now=date(2026, 8, 18),
            )


def test_prediction_clip_and_frozen_parameters():
    from pydantic import ValidationError

    from quantbot.learner.five_minute import FEATURE_NAMES
    from quantbot.paper_core.contracts import ModelArtifact
    from quantbot.paper_core.learning import predict_bps

    features = ({name: 0 for name in FEATURE_NAMES},)
    assert predict_bps(ModelArtifact.fixture_constant(200), features) == (100,)
    assert predict_bps(ModelArtifact.fixture_constant(-200), features) == (-100,)
    with pytest.raises(ValidationError):
        ModelArtifact.fixture_constant(float("inf"))
    model = ModelArtifact()
    with pytest.raises(ValidationError):
        model.mode = "PREDICT"


def test_zero_baseline_error_is_not_a_five_percent_improvement():
    from quantbot.paper_core.learning import _passes_validation

    metrics = {
        "candidate_mse": 0,
        "incumbent_mse": 0,
        "constant_mse": 0,
        "candidate_worst_session_mse": 0,
        "incumbent_worst_session_mse": 0,
        "constant_worst_session_mse": 0,
    }
    assert not _passes_validation(metrics, 0.05)


@pytest.mark.parametrize("modern", [False, True])
def test_all_history_and_legacy_sixty_session_intersections_are_metadata_only(
    tmp_path, monkeypatch, modern
):
    from datetime import UTC, datetime, timedelta

    from tests.fixtures.core_m2 import prepare

    from quantbot.paper_core import learning
    from quantbot.paper_core.contracts import LearningPolicy

    db, store, _, _, _, _ = prepare(tmp_path)
    begin = datetime(2026, 1, 1, 14, tzinfo=UTC)
    rows = []
    for day in range(61):
        start = begin + timedelta(days=day)
        rows.append(
            {
                "outcome_id": f"metadata-{day}",
                "observation_id": f"obs-{day}",
                "lot_id": f"lot-{day}",
                "dataset": "EXPLICIT_SYNTHETIC",
                "policy_id": store.config.policy_id,
                "evidence_lane": "SYNTHETIC_TEST",
                "feature_version": "five-minute-price-alpha-v1",
                "status": "COMPLETE",
                "source_start": start.isoformat(),
                "source_end": (start + timedelta(hours=1)).isoformat(),
                "available_at": (start + timedelta(hours=1)).isoformat(),
                "session_close": (start + timedelta(hours=6)).isoformat(),
            }
        )
    rows.append({**rows[-1], "outcome_id": "overlap-canary"})
    monkeypatch.setattr(learning, "_metadata", lambda session: rows)
    monkeypatch.setattr(learning, "_revision_metadata", lambda session, cutoff: rows)
    try:
        with db.transaction() as session:
            selected, excluded, considered = learning._select(
                session,
                LearningPolicy() if modern else LearningPolicy.legacy_fixture(),
                begin + timedelta(days=62),
                execution_now=begin + timedelta(days=62),
            )
            assert len(selected) == (61 if modern else 60) and len(considered) == 62
            assert (("metadata-0", "OUTSIDE_LAST_60_SESSIONS") in excluded) is (not modern)
            assert ("overlap-canary", "INTERSECTING_FEATURE_HOLDING_WINDOW") in excluded
    finally:
        db.close()
