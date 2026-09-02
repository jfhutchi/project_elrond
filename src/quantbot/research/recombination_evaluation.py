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
    """Rates used for comparison; candidate count itself is intentionally absent."""

    blocked_before_compute: Decimal
    confirmatory_per_novel_question: Decimal
    information_gain_per_confirmatory: Decimal
    burden_adjusted_survivor_rate: Decimal
    hostile_refutation_rate: Decimal
    compute_seconds_per_information_gain: Decimal | None


class ComparisonDecision(StrEnum):
    KEEP_BASELINE = "KEEP_BASELINE"
    ELIGIBLE_FOR_DEFAULT_TRIAL = "ELIGIBLE_FOR_DEFAULT_TRIAL"


class BoundedGenerationComparison(FrozenModel):
    """An immutable decision report; never a strategy or promotion verdict."""

    baseline: GenerationPathMetrics
    recombination: GenerationPathMetrics
    baseline_efficiency: GenerationEfficiency
    recombination_efficiency: GenerationEfficiency
    decision: ComparisonDecision
    reasons: tuple[Text, ...]

    @property
    def comparison_id(self) -> str:
        return content_id(self)


class ComparisonRefused(ValueError):
    """Raised when two runs do not constitute a controlled bounded comparison."""


def _ratio(numerator: int | Decimal, denominator: int | Decimal) -> Decimal:
    if denominator == 0:
        return Decimal(0)
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


def compare_generation_paths(
    baseline: GenerationPathMetrics,
    recombination: GenerationPathMetrics,
) -> BoundedGenerationComparison:
    """Keep the baseline unless a like-for-like completed comparison clears every gate."""
    if baseline.run_id == recombination.run_id:
        raise ComparisonRefused("comparison runs must have distinct identities")
    if baseline.frozen_corpus_id != recombination.frozen_corpus_id:
        raise ComparisonRefused("generator comparison requires the same frozen corpus")
    if baseline.discovery_budget != recombination.discovery_budget:
        raise ComparisonRefused("generator comparison requires the same discovery budget")

    baseline_rates = efficiency(baseline)
    recombination_rates = efficiency(recombination)
    reasons: list[str] = []
    for label, passed in (
        (
            "blocks at least as many duplicates/refutations before compute",
            recombination_rates.blocked_before_compute
            >= baseline_rates.blocked_before_compute,
        ),
        (
            "spends no more confirmatory experiments per novel question",
            recombination_rates.confirmatory_per_novel_question
            <= baseline_rates.confirmatory_per_novel_question,
        ),
        (
            "produces strictly more information per confirmatory experiment",
            recombination_rates.information_gain_per_confirmatory
            > baseline_rates.information_gain_per_confirmatory,
        ),
        (
            "survivors remain at least as frequent after search-burden charging",
            recombination_rates.burden_adjusted_survivor_rate
            >= baseline_rates.burden_adjusted_survivor_rate,
        ),
        (
            "hostile refutation rate is no worse",
            recombination_rates.hostile_refutation_rate
            <= baseline_rates.hostile_refutation_rate,
        ),
        (
            "consumes no more protected windows",
            recombination.protected_windows_consumed
            <= baseline.protected_windows_consumed,
        ),
        (
            "repeats no more known failures",
            recombination.known_failures_repeated <= baseline.known_failures_repeated,
        ),
    ):
        if not passed:
            reasons.append(label)

    baseline_hostile = (
        baseline.hostile_validation_survivors
        + baseline.hostile_validation_refutations
    )
    recombination_hostile = (
        recombination.hostile_validation_survivors
        + recombination.hostile_validation_refutations
    )
    if baseline_hostile != baseline.confirmatory_experiments or recombination_hostile != (
        recombination.confirmatory_experiments
    ):
        reasons.append("all confirmatory experiments need hostile-validation outcomes")

    baseline_compute = baseline_rates.compute_seconds_per_information_gain
    recombination_compute = recombination_rates.compute_seconds_per_information_gain
    if recombination_compute is None or (
        baseline_compute is not None and recombination_compute > baseline_compute
    ):
        reasons.append("compute per unit of information gain is no better")

    decision = (
        ComparisonDecision.ELIGIBLE_FOR_DEFAULT_TRIAL
        if not reasons
        else ComparisonDecision.KEEP_BASELINE
    )
    return BoundedGenerationComparison(
        baseline=baseline,
        recombination=recombination,
        baseline_efficiency=baseline_rates,
        recombination_efficiency=recombination_rates,
        decision=decision,
        reasons=tuple(reasons),
    )


__all__ = [
    "BoundedGenerationComparison",
    "ComparisonDecision",
    "ComparisonRefused",
    "GenerationEfficiency",
    "GenerationPathMetrics",
    "compare_generation_paths",
    "efficiency",
]
