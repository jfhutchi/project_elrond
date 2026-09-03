"""The recombination path must earn default use on value, not idea volume."""

from decimal import Decimal

import pytest

from quantbot.research.recombination_evaluation import (
    ComparisonDecision,
    ComparisonRefused,
    GenerationPathMetrics,
    MetricOutcome,
    compare_generation_paths,
    efficiency,
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


# ----------------------------------------------------------------------------------------
# Zero denominators.
#
# `_ratio` returned `Decimal(0)` for `0/0`, which is indistinguishable from a rate somebody
# measured. The consequences ran in both directions from that one fabricated number: on the
# gates where low is good it made a path that did nothing unbeatable, and on the single gate
# where high is good it made an empty baseline free to beat. Neither is a comparison.
#
# The rule now: an undefined rate is `None`, an axis with an undefined side is NOT_COMPARABLE,
# and NOT_COMPARABLE blocks eligibility without being recorded as recombination doing worse.
# ----------------------------------------------------------------------------------------


def productive(**overrides: object) -> GenerationPathMetrics:
    """A recombination run that found questions, tested some, and validated them hostilely."""
    fields: dict[str, object] = {
        "run_id": "recombination-v1",
        "path": "evidence-aware-recombination",
        "duplicates_blocked": 2,
        "known_refutations_blocked": 2,
        "genuinely_novel_questions": 6,
        "confirmatory_experiments": 3,
        "information_gain": Decimal("3"),
        "hostile_validation_survivors": 2,
        "hostile_validation_refutations": 1,
        "compute_seconds": Decimal("75"),
        "protected_windows_consumed": 0,
        "known_failures_repeated": 0,
    }
    fields.update(overrides)
    return metrics(**fields)


def idle(**overrides: object) -> GenerationPathMetrics:
    """A path that examined its candidates and produced nothing downstream.

    Deliberately legal: every accounting invariant holds, so the model has no reason to refuse
    it and the comparison has to cope. That is what made the old behaviour dangerous -- this is
    not a malformed input, it is a run that found nothing.
    """
    fields: dict[str, object] = {
        "run_id": "idle-baseline",
        "path": "existing-generator",
        "duplicates_blocked": 0,
        "known_refutations_blocked": 0,
        "genuinely_novel_questions": 0,
        "confirmatory_experiments": 0,
        "information_gain": Decimal("0"),
        "hostile_validation_survivors": 0,
        "hostile_validation_refutations": 0,
        "compute_seconds": Decimal("50"),
        "protected_windows_consumed": 0,
        "known_failures_repeated": 0,
    }
    fields.update(overrides)
    return metrics(**fields)


def axis(report, metric: str):
    return next(item for item in report.comparisons if item.metric == metric)


def test_an_undefined_rate_is_none_and_never_a_measured_zero() -> None:
    """The root of it. `0/0` has no value, and `Decimal(0)` is a claim about a path that did
    the work and scored nothing."""
    rates = efficiency(idle())

    assert rates.confirmatory_per_novel_question is None
    assert rates.information_gain_per_confirmatory is None
    assert rates.hostile_refutation_rate is None
    assert rates.compute_seconds_per_information_gain is None
    # The control: denominators the model guarantees are non-zero stay real numbers, so this is
    # distinguishing undefined from zero rather than turning every rate into None.
    assert rates.blocked_before_compute == Decimal(0)
    assert rates.burden_adjusted_survivor_rate == Decimal(0)


def test_a_baseline_that_did_nothing_does_not_win_by_default() -> None:
    """**The defect.** A baseline with no novel questions and no confirmatory experiments held
    a fabricated `0` on every rate, and `recombination <= baseline` cannot be satisfied by any
    real rate -- so a recombination run that found legitimate questions and tested some of them
    lost to a run that did nothing.

    It must not lose, and it must not win either: the evidence to rank them does not exist.
    """
    report = compare_generation_paths(idle(), productive())

    assert report.decision is ComparisonDecision.COMPARISON_NOT_POSSIBLE
    assert not report.eligible
    # Not recorded as recombination being worse on any axis. That is the claim the fabricated
    # zero was making, and neither run supports it.
    assert not any(item.outcome is MetricOutcome.FAILED for item in report.comparisons)
    assert any("the baseline produced no" in reason for reason in report.reasons)


def test_a_recombination_that_did_nothing_cannot_become_eligible() -> None:
    """Fail closed in the other direction. An empty recombination run has no rate to show an
    improvement with, and must not inherit eligibility from the absence of a contrary number."""
    report = compare_generation_paths(
        metrics(protected_windows_consumed=5, known_failures_repeated=5),
        idle(run_id="idle-recombination", path="evidence-aware-recombination"),
    )

    assert report.decision is not ComparisonDecision.ELIGIBLE_FOR_DEFAULT_TRIAL
    assert not report.eligible
    assert any("recombination produced no" in reason for reason in report.reasons)


def test_neither_path_producing_a_denominator_decides_nothing() -> None:
    """Both empty: the axis distinguishes nothing, so it cannot be a pass."""
    report = compare_generation_paths(
        idle(), idle(run_id="idle-recombination", path="evidence-aware-recombination")
    )

    assert report.decision is ComparisonDecision.COMPARISON_NOT_POSSIBLE
    assert not report.eligible
    assert any("neither path produced any" in reason for reason in report.reasons)


def test_zero_hostile_outcomes_do_not_become_a_perfect_refutation_rate() -> None:
    """A path with no hostile-validation outcomes has no hostile refutation rate. Read as a
    measured `0`, it was the best possible score on a gate where lower is better -- so never
    running hostile validation beat running it and refuting things."""
    assert efficiency(idle()).hostile_refutation_rate is None

    hostile = axis(
        compare_generation_paths(idle(), productive()), "hostile_refutation_rate"
    )

    assert hostile.outcome is MetricOutcome.NOT_COMPARABLE
    assert hostile.baseline is None
    assert hostile.recombination is not None


def test_an_empty_baseline_does_not_hand_out_the_information_gain_gate() -> None:
    """The opposite-direction half of the same defect, and the one that could have *promoted*.

    `information_gain_per_confirmatory` requires recombination to be strictly greater. With the
    baseline fabricating `0`, any positive information gain cleared it, so an empty baseline
    made the hardest gate in the comparison free.
    """
    gain = axis(
        compare_generation_paths(idle(), productive()),
        "information_gain_per_confirmatory",
    )

    assert gain.outcome is MetricOutcome.NOT_COMPARABLE
    assert gain.baseline is None


def test_an_empty_baseline_does_not_hand_out_the_compute_gate() -> None:
    """Compute per unit of information gain was already `None` when information gain was zero,
    and the gate read an absent *baseline* as satisfied. A recombination run at any compute cost
    cleared it, provided the baseline had produced nothing to compare against."""
    compute = axis(
        compare_generation_paths(idle(), productive()),
        "compute_seconds_per_information_gain",
    )

    assert compute.outcome is MetricOutcome.NOT_COMPARABLE
    assert compute.baseline is None


def test_a_genuine_zero_still_compares_normally() -> None:
    """The control that keeps the fix honest. A path that ran confirmatory experiments and
    gained no information has a *measured* rate of zero, and zero must still compare -- this
    distinguishes undefined from zero rather than treating every zero as suspect."""
    baseline = metrics(information_gain=Decimal("0"), compute_seconds=Decimal("0"))
    assert efficiency(baseline).information_gain_per_confirmatory == Decimal(0)

    gain = axis(
        compare_generation_paths(baseline, productive(protected_windows_consumed=1)),
        "information_gain_per_confirmatory",
    )

    assert gain.outcome is MetricOutcome.PASSED
    assert gain.baseline == Decimal(0)


def test_a_real_failure_outranks_an_unevaluated_axis() -> None:
    """When one axis measured recombination as worse and another could not be evaluated, the
    measurement wins: `KEEP_BASELINE` states something the evidence supports, and
    `COMPARISON_NOT_POSSIBLE` would understate it."""
    report = compare_generation_paths(
        idle(),
        idle(
            run_id="idle-recombination",
            path="evidence-aware-recombination",
            protected_windows_consumed=5,
        ),
    )

    assert report.decision is ComparisonDecision.KEEP_BASELINE
    assert "consumes no more protected windows" in report.reasons


def test_an_unevaluated_axis_never_reads_as_a_clean_sweep() -> None:
    """`reasons` is the field a caller is most likely to check. An empty one has to keep
    meaning "every gate passed", so a not-comparable axis must appear in it -- distinguishably,
    which is what the prefix is for."""
    report = compare_generation_paths(idle(), productive())

    assert report.reasons != ()
    assert all(reason.startswith("not comparable: ") for reason in report.reasons)


def test_the_decision_still_grants_at_most_a_default_trial() -> None:
    """Nothing here is evidence of alpha, and no outcome may say otherwise. Pinned so a fourth
    decision cannot be added without someone reading this line."""
    assert {decision.value for decision in ComparisonDecision} == {
        "KEEP_BASELINE",
        "COMPARISON_NOT_POSSIBLE",
        "ELIGIBLE_FOR_DEFAULT_TRIAL",
    }
