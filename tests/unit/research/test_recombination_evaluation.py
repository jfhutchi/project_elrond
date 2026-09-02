"""The recombination path must earn default use on value, not idea volume."""

from decimal import Decimal

import pytest

from quantbot.research.recombination_evaluation import (
    ComparisonDecision,
    ComparisonRefused,
    GenerationPathMetrics,
    compare_generation_paths,
)

CORPUS = "a" * 64


def metrics(**overrides: object) -> GenerationPathMetrics:
    fields: dict[str, object] = {
        "run_id": "baseline-v1",
        "path": "existing-generator",
        "generator_version": "1",
        "frozen_corpus_id": CORPUS,
        "discovery_budget": 10,
        "candidates_examined": 10,
        "duplicates_blocked": 1,
        "known_refutations_blocked": 1,
        "genuinely_novel_questions": 8,
        "confirmatory_experiments": 4,
        "information_gain": Decimal("2"),
        "hostile_validation_survivors": 1,
        "hostile_validation_refutations": 3,
        "compute_seconds": Decimal("100"),
        "protected_windows_consumed": 2,
        "known_failures_repeated": 1,
        "search_trials_charged": 10,
    }
    fields.update(overrides)
    return GenerationPathMetrics(**fields)


def test_comparison_requires_the_same_frozen_corpus_and_budget() -> None:
    baseline = metrics()
    with pytest.raises(ComparisonRefused, match="same frozen corpus"):
        compare_generation_paths(
            baseline,
            metrics(run_id="recombination-v1", frozen_corpus_id="b" * 64),
        )
    with pytest.raises(ComparisonRefused, match="same discovery budget"):
        compare_generation_paths(
            baseline,
            metrics(run_id="recombination-v1", discovery_budget=20),
        )


def test_generating_more_candidates_cannot_make_recombination_win() -> None:
    baseline = metrics(discovery_budget=20)
    recombination = metrics(
        run_id="recombination-v1",
        path="evidence-aware-recombination",
        discovery_budget=20,
        candidates_examined=20,
        search_trials_charged=20,
        genuinely_novel_questions=8,
    )

    report = compare_generation_paths(baseline, recombination)

    assert report.decision is ComparisonDecision.KEEP_BASELINE
    assert "blocks at least as many duplicates/refutations before compute" in report.reasons
    assert report.comparison_id == report.model_copy().comparison_id


def test_recombination_only_earns_a_default_trial_when_every_efficiency_gate_clears() -> None:
    baseline = metrics()
    recombination = metrics(
        run_id="recombination-v1",
        path="evidence-aware-recombination",
        duplicates_blocked=2,
        known_refutations_blocked=2,
        genuinely_novel_questions=6,
        confirmatory_experiments=3,
        information_gain=Decimal("3"),
        hostile_validation_survivors=2,
        hostile_validation_refutations=1,
        compute_seconds=Decimal("75"),
        protected_windows_consumed=1,
        known_failures_repeated=0,
    )

    report = compare_generation_paths(baseline, recombination)

    assert report.decision is ComparisonDecision.ELIGIBLE_FOR_DEFAULT_TRIAL
    assert report.reasons == ()
    assert (
        report.recombination_efficiency.information_gain_per_confirmatory
        > report.baseline_efficiency.information_gain_per_confirmatory
    )


def test_search_burden_cannot_undercharge_examined_candidates() -> None:
    with pytest.raises(ValueError, match="every candidate examined"):
        metrics(search_trials_charged=9)
