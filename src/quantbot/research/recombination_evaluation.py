"""Conservative, frozen-corpus evaluation for candidate-generation paths (#189).

Generating more ideas is not research progress.  This module compares observed outcomes from
two bounded generator runs on the same corpus and budget.  It reports every issue-required
efficiency measure and keeps the baseline unless recombination is better on information value
without being worse on search burden, hostile validation, compute, protected data, or repeated
known failures.

The result is only eligibility for a default-path trial.  It is not evidence of market alpha and
cannot promote a hypothesis or authorize execution.
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from quantbot.research.manifest import content_id

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GenerationPathMetrics(FrozenModel):
    """Observed results from one generator on one frozen corpus and discovery budget."""

    schema_version: int = Field(default=1, ge=1, le=1)
    run_id: Text
    path: Text
    generator_version: Text
    frozen_corpus_id: Digest
    discovery_budget: int = Field(ge=1)
    candidates_examined: int = Field(ge=1)
    duplicates_blocked: int = Field(ge=0)
    known_refutations_blocked: int = Field(ge=0)
    genuinely_novel_questions: int = Field(ge=0)
    confirmatory_experiments: int = Field(ge=0)
    information_gain: Decimal = Field(ge=0)
    hostile_validation_survivors: int = Field(ge=0)
    hostile_validation_refutations: int = Field(ge=0)
    compute_seconds: Decimal = Field(ge=0)
    protected_windows_consumed: int = Field(ge=0)
    known_failures_repeated: int = Field(ge=0)
    search_trials_charged: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_accounting(self) -> Self:
        blocked = self.duplicates_blocked + self.known_refutations_blocked
        if self.candidates_examined > self.discovery_budget:
            raise ValueError("candidates examined exceed the frozen discovery budget")
        if blocked > self.candidates_examined:
            raise ValueError("pre-compute blocks cannot exceed candidates examined")
        if self.genuinely_novel_questions > self.candidates_examined - blocked:
            raise ValueError("novel questions must exclude duplicates and known refutations")
        if self.confirmatory_experiments > self.genuinely_novel_questions:
            raise ValueError("confirmatory experiments cannot exceed genuinely novel questions")
        hostile = (
            self.hostile_validation_survivors + self.hostile_validation_refutations
        )
        if hostile > self.confirmatory_experiments:
            raise ValueError("hostile outcomes cannot exceed confirmatory experiments")
        if self.known_failures_repeated > self.candidates_examined:
            raise ValueError("repeated failures cannot exceed candidates examined")
        if self.search_trials_charged < self.candidates_examined:
            raise ValueError("every candidate examined must remain charged to search burden")
        return self

    @property
    def metrics_id(self) -> str:
        return content_id(self)


class GenerationEfficiency(FrozenModel):
    """Rates used for comparison; candidate count itself is intentionally absent.

    **`None` means undefined, and is not a zero.** Every field here is a ratio, and a ratio whose
    denominator is zero has no value -- a path that ran no confirmatory experiments has no
    information gain *per* confirmatory experiment, and saying it scored `0` invents an
    observation nobody made.

    `_ratio` returned `Decimal(0)` for `0/0`, and that number then competed in the comparison as
    though it were measured. It made a path that did nothing look unbeatable on the gates where
    low is good, and trivially beatable on the one gate where high is good -- so the same
    fabricated zero pushed the decision in whichever direction the inequality happened to point.
    """

    blocked_before_compute: Decimal | None
    confirmatory_per_novel_question: Decimal | None
    information_gain_per_confirmatory: Decimal | None
    burden_adjusted_survivor_rate: Decimal | None
    hostile_refutation_rate: Decimal | None
    compute_seconds_per_information_gain: Decimal | None


class MetricOutcome(StrEnum):
    """What one axis of the comparison established."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    #: Neither. At least one side has no value on this axis, so the two cannot be ranked on it.
    #: Distinct from FAILED on purpose: "recombination was worse" and "nothing here can say"
    #: are different findings, and collapsing them is how an absent denominator becomes a win.
    NOT_COMPARABLE = "NOT_COMPARABLE"


class MetricComparison(FrozenModel):
    """One axis, both observed values, and what the pair established."""

    metric: Text
    outcome: MetricOutcome
    baseline: Decimal | None
    recombination: Decimal | None
    #: The rule this axis applies, phrased as what recombination has to do. Held separately from
    #: `detail` so `reasons` can keep carrying exactly the labels it carried before, while the
    #: numbers behind a decision live here rather than being lost.
    requirement: Text
    detail: Text


class ComparisonDecision(StrEnum):
    KEEP_BASELINE = "KEEP_BASELINE"
    #: No axis said recombination is worse, and at least one could not be evaluated. Grants
    #: nothing, exactly like KEEP_BASELINE, and says something different: that the *evidence* was
    #: missing rather than unfavourable. A baseline that produced no denominators has not won a
    #: comparison, it has failed to participate in one, and a reader deciding whether to re-run
    #: with a working baseline needs to be able to tell those apart.
    COMPARISON_NOT_POSSIBLE = "COMPARISON_NOT_POSSIBLE"
    ELIGIBLE_FOR_DEFAULT_TRIAL = "ELIGIBLE_FOR_DEFAULT_TRIAL"


class BoundedGenerationComparison(FrozenModel):
    """An immutable decision report; never a strategy or promotion verdict."""

    baseline: GenerationPathMetrics
    recombination: GenerationPathMetrics
    baseline_efficiency: GenerationEfficiency
    recombination_efficiency: GenerationEfficiency
    decision: ComparisonDecision
    #: Every axis with its two observed values, including the ones that decided nothing.
    comparisons: tuple[MetricComparison, ...]
    reasons: tuple[Text, ...]

    @property
    def comparison_id(self) -> str:
        return content_id(self)

    @property
    def eligible(self) -> bool:
        return self.decision is ComparisonDecision.ELIGIBLE_FOR_DEFAULT_TRIAL


class ComparisonRefused(ValueError):
    """Raised when two runs do not constitute a controlled bounded comparison."""


def _ratio(numerator: int | Decimal, denominator: int | Decimal) -> Decimal | None:
    """`None` when the denominator is zero, because the rate does not exist.

    The previous `Decimal(0)` was the defect: it is indistinguishable from a real measured zero,
    and a real measured zero is a claim about a path that actually did the work.
    """
    if denominator == 0:
        return None
    return Decimal(numerator) / Decimal(denominator)


def efficiency(metrics: GenerationPathMetrics) -> GenerationEfficiency:
    """Expose all required rates without smoothing away zero-observation runs."""
    hostile = (
        metrics.hostile_validation_survivors
        + metrics.hostile_validation_refutations
    )
    return GenerationEfficiency(
        blocked_before_compute=_ratio(
            metrics.duplicates_blocked + metrics.known_refutations_blocked,
            metrics.candidates_examined,
        ),
        confirmatory_per_novel_question=_ratio(
            metrics.confirmatory_experiments, metrics.genuinely_novel_questions
        ),
        information_gain_per_confirmatory=_ratio(
            metrics.information_gain, metrics.confirmatory_experiments
        ),
        burden_adjusted_survivor_rate=_ratio(
            metrics.hostile_validation_survivors, metrics.search_trials_charged
        ),
        hostile_refutation_rate=_ratio(
            metrics.hostile_validation_refutations, hostile
        ),
        compute_seconds_per_information_gain=(
            None
            if metrics.information_gain == 0
            else metrics.compute_seconds / metrics.information_gain
        ),
    )


def _compare_rate(
    metric: str,
    requirement: str,
    denominator: str,
    baseline: Decimal | None,
    recombination: Decimal | None,
    better: Callable[[Decimal, Decimal], bool],
) -> MetricComparison:
    """Rank two paths on one rate, or say plainly that they cannot be ranked.

    The four cases, defined rather than left to fall out of the arithmetic:

    * **Both defined** -- ordinary comparison against `better`.
    * **Only the baseline undefined** -- the baseline produced no `denominator`, so there is no
      value for recombination to be better than. `NOT_COMPARABLE`. It is *not* a pass: passing
      would mean an empty baseline hands out eligibility on every axis it failed to measure. It
      is *not* a fail either, which is the half the previous code got wrong -- a fabricated
      baseline zero made the do-nothing run the winner.
    * **Only recombination undefined** -- recombination produced no `denominator`, so it cannot
      demonstrate an improvement. `NOT_COMPARABLE`, and it blocks, which is the fail-closed
      direction.
    * **Neither defined** -- the axis distinguishes nothing about either path.
      `NOT_COMPARABLE`.

    Every undefined case blocks eligibility. None of them is recorded as recombination being
    worse, because that is a claim neither run supports.
    """
    if baseline is None and recombination is None:
        return MetricComparison(
            metric=metric,
            outcome=MetricOutcome.NOT_COMPARABLE,
            baseline=None,
            recombination=None,
            requirement=requirement,
            detail=(
                f"neither path produced any {denominator}, so {metric} is undefined for both "
                "and distinguishes nothing"
            ),
        )
    if baseline is None:
        return MetricComparison(
            metric=metric,
            outcome=MetricOutcome.NOT_COMPARABLE,
            baseline=None,
            recombination=recombination,
            requirement=requirement,
            detail=(
                f"the baseline produced no {denominator}, so {metric} is undefined for it and "
                f"there is no measured rate for recombination's {recombination} to beat; run a "
                "baseline that reaches this axis rather than reading its absence as a score"
            ),
        )
    if recombination is None:
        return MetricComparison(
            metric=metric,
            outcome=MetricOutcome.NOT_COMPARABLE,
            baseline=baseline,
            recombination=None,
            requirement=requirement,
            detail=(
                f"recombination produced no {denominator}, so {metric} is undefined for it and "
                f"it cannot show it {requirement} against the baseline's {baseline}"
            ),
        )
    passed = better(recombination, baseline)
    return MetricComparison(
        metric=metric,
        outcome=MetricOutcome.PASSED if passed else MetricOutcome.FAILED,
        baseline=baseline,
        recombination=recombination,
        requirement=requirement,
        detail=f"recombination {recombination} against baseline {baseline}",
    )


def _compare_count(
    metric: str, requirement: str, baseline: int, recombination: int
) -> MetricComparison:
    """Counts are always defined, so these can only pass or fail."""
    passed = recombination <= baseline
    return MetricComparison(
        metric=metric,
        outcome=MetricOutcome.PASSED if passed else MetricOutcome.FAILED,
        baseline=Decimal(baseline),
        recombination=Decimal(recombination),
        requirement=requirement,
        detail=f"recombination {recombination} against baseline {baseline}",
    )


def compare_generation_paths(
    baseline: GenerationPathMetrics,
    recombination: GenerationPathMetrics,
) -> BoundedGenerationComparison:
    """Keep the baseline unless a like-for-like completed comparison clears every gate.

    Three outcomes, and the middle one is the point of this function's design: `KEEP_BASELINE`
    means an axis measured recombination as worse; `COMPARISON_NOT_POSSIBLE` means no axis said
    that and at least one could not be evaluated at all. Both grant exactly nothing. Reporting
    them as the same thing would let a baseline that ran nothing be recorded as having won.
    """
    if baseline.run_id == recombination.run_id:
        raise ComparisonRefused("comparison runs must have distinct identities")
    if baseline.frozen_corpus_id != recombination.frozen_corpus_id:
        raise ComparisonRefused("generator comparison requires the same frozen corpus")
    if baseline.discovery_budget != recombination.discovery_budget:
        raise ComparisonRefused("generator comparison requires the same discovery budget")

    baseline_rates = efficiency(baseline)
    recombination_rates = efficiency(recombination)
    at_least = operator.ge
    at_most = operator.le
    strictly_more = operator.gt

    comparisons: list[MetricComparison] = [
        _compare_rate(
            "blocked_before_compute",
            "blocks at least as many duplicates/refutations before compute",
            "candidates examined",
            baseline_rates.blocked_before_compute,
            recombination_rates.blocked_before_compute,
            at_least,
        ),
        _compare_rate(
            "confirmatory_per_novel_question",
            "spends no more confirmatory experiments per novel question",
            "genuinely novel questions",
            baseline_rates.confirmatory_per_novel_question,
            recombination_rates.confirmatory_per_novel_question,
            at_most,
        ),
        _compare_rate(
            "information_gain_per_confirmatory",
            "produces strictly more information per confirmatory experiment",
            "confirmatory experiments",
            baseline_rates.information_gain_per_confirmatory,
            recombination_rates.information_gain_per_confirmatory,
            strictly_more,
        ),
        _compare_rate(
            "burden_adjusted_survivor_rate",
            "keeps survivors at least as frequent after search-burden charging",
            "charged search trials",
            baseline_rates.burden_adjusted_survivor_rate,
            recombination_rates.burden_adjusted_survivor_rate,
            at_least,
        ),
        _compare_rate(
            "hostile_refutation_rate",
            "holds a hostile refutation rate no worse than the baseline's",
            "hostile-validation outcomes",
            baseline_rates.hostile_refutation_rate,
            recombination_rates.hostile_refutation_rate,
            at_most,
        ),
        _compare_rate(
            "compute_seconds_per_information_gain",
            "spends no more compute per unit of information gain",
            "information gain",
            baseline_rates.compute_seconds_per_information_gain,
            recombination_rates.compute_seconds_per_information_gain,
            at_most,
        ),
        _compare_count(
            "protected_windows_consumed",
            "consumes no more protected windows",
            baseline.protected_windows_consumed,
            recombination.protected_windows_consumed,
        ),
        _compare_count(
            "known_failures_repeated",
            "repeats no more known failures",
            baseline.known_failures_repeated,
            recombination.known_failures_repeated,
        ),
    ]

    # Failure reasons are the bare requirement labels, unchanged from before this module grew a
    # third outcome: a caller matching on them keeps working. The observed numbers behind each
    # decision live on `comparisons` instead of being interpolated into the label.
    reasons = [
        item.requirement for item in comparisons if item.outcome is MetricOutcome.FAILED
    ]
    not_comparable = [
        f"not comparable: {item.detail}"
        for item in comparisons
        if item.outcome is MetricOutcome.NOT_COMPARABLE
    ]

    # Unchanged, and still a hard requirement: an experiment nobody hostile-validated cannot be
    # counted as one that survived hostility. Kept out of the rate machinery because it is an
    # accounting completeness check rather than a comparison between the two paths.
    baseline_hostile = (
        baseline.hostile_validation_survivors + baseline.hostile_validation_refutations
    )
    recombination_hostile = (
        recombination.hostile_validation_survivors
        + recombination.hostile_validation_refutations
    )
    if baseline_hostile != baseline.confirmatory_experiments or recombination_hostile != (
        recombination.confirmatory_experiments
    ):
        reasons.append("all confirmatory experiments need hostile-validation outcomes")

    if reasons:
        decision = ComparisonDecision.KEEP_BASELINE
    elif not_comparable:
        decision = ComparisonDecision.COMPARISON_NOT_POSSIBLE
    else:
        decision = ComparisonDecision.ELIGIBLE_FOR_DEFAULT_TRIAL

    return BoundedGenerationComparison(
        baseline=baseline,
        recombination=recombination,
        baseline_efficiency=baseline_rates,
        recombination_efficiency=recombination_rates,
        decision=decision,
        comparisons=tuple(comparisons),
        # Failures first, then the axes that established nothing. Both are in `reasons` because a
        # caller reading only that field must not be able to mistake an unevaluated comparison
        # for a clean sweep -- an empty `reasons` has to keep meaning "every gate passed".
        reasons=tuple((*reasons, *not_comparable)),
    )


__all__ = [
    "BoundedGenerationComparison",
    "ComparisonDecision",
    "ComparisonRefused",
    "GenerationEfficiency",
    "GenerationPathMetrics",
    "MetricComparison",
    "MetricOutcome",
    "compare_generation_paths",
    "efficiency",
]
