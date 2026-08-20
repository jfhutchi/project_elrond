"""Compile a frozen registration into an experiment that can only be run honestly (#8).

Cycles 11-12 hand-wrote each hypothesis into a script, and that is exactly where two of the
three analysis errors entered. This closes that seam, and three things about how it closes it
are load-bearing.

**The statistic is selected from the declared dependence structure, not from a default.**
Compiling "compare the Sharpe of A and B" into an independent-samples test is a faithful
compilation and a wrong experiment when A and B are the same asset at different exposures --
that one measured z=1.01 where it had appeared to be a clear win. The mapping is small and
mechanical, and a pair it cannot resolve is refused rather than defaulted.

**The adversarial probes are emitted with the primary test, not after it.** In cycles 11-12
every probe was written by hand once a result already looked good, which is the wrong time to
decide to run them. Compiled from the registration, they run whether or not anyone suspects a
problem, and an outcome that omits one cannot be recorded as a survivor.

**A confirmatory result must come from the production evaluation path.** Reproducibility checks
a result against itself and misses the result that is perfectly reproducible and still wrong
because it never ran through the system. Commit `f77ea08` is the case: a standalone study
concluded a 2500bps exposure floor turned $100 into $252.43 against $126.80, deterministic and
reproducible, and wrong about Elrond -- it had measured the halt policy against SPY-200-day
returns while the engine runs the momentum rotation. Both numbers were correct; only one was
about this system. So a study that bypasses `BacktestEngine` cannot produce a survivor here; it
can only produce a standalone model, labelled as one.

Nothing is discarded. A refused compilation, a failed run, and an underpowered result are all
recorded outcomes, because "we tried this and it did not work" and "we never tried this" are
different facts and #6 depends on being able to tell them apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from quantbot.research.manifest import (
    VALID_FOR,
    DatasetSnapshot,
    ExecutionPath,
    StatisticalPlan,
    StatisticalTest,
)
from quantbot.research.power import ComparisonStructure, Estimand, PowerVerdict
from quantbot.research.registry import Registration

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

#: The statistic implied by an estimand and the dependence structure of its data. A pair absent
#: from this table is refused rather than defaulted: the default is what produced cycle 11's
#: independent-samples comparison of two series of the same asset.
TEST_FOR: dict[tuple[Estimand, ComparisonStructure], StatisticalTest] = {
    # Comparing Sharpes of two correlated series is the case a t-test gets wrong.
    (Estimand.SHARPE, ComparisonStructure.PAIRED): StatisticalTest.JOBSON_KORKIE_MEMMEL,
    (Estimand.SHARPE, ComparisonStructure.UNPAIRED): StatisticalTest.WELCH_T,
    (Estimand.SHARPE, ComparisonStructure.SINGLE_SAMPLE): StatisticalTest.ONE_SAMPLE_T,
    (Estimand.MEAN_DIFFERENCE, ComparisonStructure.PAIRED): StatisticalTest.PAIRED_T,
    (Estimand.MEAN_DIFFERENCE, ComparisonStructure.UNPAIRED): StatisticalTest.WELCH_T,
    (Estimand.MEAN_DIFFERENCE, ComparisonStructure.SINGLE_SAMPLE): StatisticalTest.ONE_SAMPLE_T,
    (Estimand.CROSS_SECTIONAL_SPREAD, ComparisonStructure.PAIRED): StatisticalTest.PAIRED_T,
    (Estimand.CROSS_SECTIONAL_SPREAD, ComparisonStructure.UNPAIRED): StatisticalTest.WELCH_T,
    (
        Estimand.CROSS_SECTIONAL_SPREAD,
        ComparisonStructure.SINGLE_SAMPLE,
    ): StatisticalTest.ONE_SAMPLE_T,
    (Estimand.HIT_RATE, ComparisonStructure.SINGLE_SAMPLE): StatisticalTest.BINOMIAL,
    (
        Estimand.INFORMATION_COEFFICIENT,
        ComparisonStructure.SINGLE_SAMPLE,
    ): StatisticalTest.SPEARMAN,
}

#: Probes every compiled experiment runs, whatever the primary result looks like. Each is a
#: check from #7 that would have caught a real failure here.
REQUIRED_PROBES: tuple[str, ...] = (
    "shifted-feature-probe",
    "cost-ladder",
    "regime-split",
    "hidden-beta",
)

#: Added when the universe has more than one instrument, because a mechanism claim over several
#: assets implies the mechanism works on more than the one it was found on.
GENERALITY_PROBE = "cross-asset-generality"


class CompilationRefused(ValueError):
    """Raised when a registration cannot be turned into a valid experiment."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


class OutcomeVerdict(StrEnum):
    SURVIVED = "SURVIVED"
    REFUTED = "REFUTED"
    #: The run happened and could not resolve the claim. Distinct from REFUTED, per #19.
    UNDERPOWERED = "UNDERPOWERED"
    #: Ran, but something about the run means the number cannot be read as evidence.
    INCONCLUSIVE = "INCONCLUSIVE"
    #: The experiment did not complete. Recorded, because a failure is not an absence.
    FAILED = "FAILED"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def select_test(
    estimand: Estimand, comparison: ComparisonStructure, *, path_dependent: bool = False
) -> StatisticalTest:
    """Choose the statistic the declared structure actually supports, or refuse.

    A path-dependent quantity -- terminal wealth, maximum drawdown -- is not a mean, so no
    t-test describes it. Cycle 12 used a stationary bootstrap for exactly this and it is what
    turned an apparent doubling into a confidence interval spanning zero.
    """
    if path_dependent:
        return StatisticalTest.STATIONARY_BOOTSTRAP
    chosen = TEST_FOR.get((estimand, comparison))
    if chosen is None:
        raise CompilationRefused(
            "NO_VALID_TEST",
            f"a {comparison.value} comparison of {estimand.value} has no test in this "
            "compiler; declare a structure it can resolve, or extend the mapping deliberately",
        )
    if comparison.value not in VALID_FOR[chosen]:
        raise CompilationRefused(
            "NO_VALID_TEST",
            f"{chosen.value} is not valid for {comparison.value} data",
        )
    return chosen


class ExperimentPlan(FrozenModel):
    """Everything a run must do, fixed before it runs so the probes cannot be dropped later."""

    hypothesis_id: Text
    hypothesis_version: int = Field(ge=1)
    registration_hash: Text = Field(min_length=64, max_length=64)
    statistics: StatisticalPlan
    datasets: tuple[DatasetSnapshot, ...] = Field(min_length=1)
    costs: dict[str, str]
    probes: tuple[Text, ...] = Field(min_length=1)
    #: A confirmatory result has to come from the engine that will actually trade.
    execution_path: ExecutionPath

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        missing = [probe for probe in REQUIRED_PROBES if probe not in self.probes]
        if missing:
            raise ValueError(f"a compiled experiment must run {', '.join(missing)}")
        return self


def compile_experiment(
    registration: Registration,
    *,
    datasets: Sequence[DatasetSnapshot],
    execution_path: ExecutionPath = ExecutionPath.PRODUCTION_ENGINE,
    path_dependent: bool = False,
) -> ExperimentPlan:
    """Turn a frozen registration into a plan, refusing what cannot be run honestly."""
    draft = registration.draft
    if registration.power.verdict is PowerVerdict.UNECONOMIC:
        raise CompilationRefused(
            "UNECONOMIC",
            f"{draft.hypothesis_id} cannot pay its own trading costs; there is nothing to run",
        )

    declared = {(window.dataset, window.role.value) for window in draft.windows}
    supplied = {(snapshot.dataset, snapshot.role) for snapshot in datasets}
    if not declared <= supplied:
        raise CompilationRefused(
            "MISSING_DATASET",
            "the registration declares "
            + ", ".join(f"{name}/{role}" for name, role in sorted(declared - supplied))
            + " and the run does not supply it",
        )
    unavailable = [
        snapshot.dataset for snapshot in datasets if snapshot.earliest_available_at is None
    ]
    if unavailable:
        raise CompilationRefused(
            "NO_AVAILABILITY",
            f"{', '.join(sorted(unavailable))} carries no availability timestamp, so the run "
            "cannot show it avoided look-ahead",
        )

    test = select_test(
        draft.effect.estimand, draft.effect.comparison, path_dependent=path_dependent
    )
    probes = list(REQUIRED_PROBES)
    if len(draft.universe) > 1:
        probes.append(GENERALITY_PROBE)

    economics = draft.effect.economics
    return ExperimentPlan(
        hypothesis_id=draft.hypothesis_id,
        hypothesis_version=draft.version,
        registration_hash=registration.content_hash,
        statistics=StatisticalPlan(
            test=test,
            comparison=draft.effect.comparison.value,
            primary_estimand=draft.primary_estimand,
            cumulative_trials=registration.cumulative_trials,
            luck_threshold_z=registration.power.luck_threshold_z,
            minimum_detectable_effect=registration.power.minimum_detectable_effect,
            variance_inflation=registration.power.variance_inflation,
            multiple_testing_correction="expected maximum over cumulative trials",
            assumptions=(
                f"dependence: {draft.effect.comparison.value}",
                f"sampling: {draft.effect.dependence.sampling.value}",
                f"lag-1 autocorrelation: {draft.effect.dependence.lag_one_autocorrelation}",
                f"cluster size: {draft.effect.dependence.cluster_size}",
                f"horizon observations: {draft.effect.dependence.horizon_observations}",
            ),
        ),
        datasets=tuple(datasets),
        costs=(
            {
                "round_trip_cost_bps": str(economics.round_trip_cost_bps),
                "annual_rebalances": str(economics.annual_rebalances),
            }
            if economics is not None
            else {"round_trip_cost_bps": "0", "annual_rebalances": "0"}
        ),
        probes=tuple(probes),
        execution_path=execution_path,
    )


class ExperimentOutcome(FrozenModel):
    """A standardized result, which cannot claim more than the run supports."""

    plan: ExperimentPlan
    verdict: OutcomeVerdict
    #: In the estimand's own units.
    effect: Decimal | None = None
    confidence_low: Decimal | None = None
    confidence_high: Decimal | None = None
    test_statistic: Decimal | None = None
    probes_run: tuple[Text, ...] = ()
    #: Probes that ran and objected. A survivor cannot have any.
    probes_failed: tuple[Text, ...] = ()
    diagnostics: dict[str, str] = Field(default_factory=dict)
    detail: Text
    completed_at: datetime

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.verdict is OutcomeVerdict.FAILED:
            return self

        missing = [probe for probe in self.plan.probes if probe not in self.probes_run]
        if missing:
            raise ValueError(
                "an experiment that did not run its adversarial probes has no readable "
                f"result: {', '.join(missing)}"
            )
        if self.verdict is OutcomeVerdict.SURVIVED:
            if self.probes_failed:
                raise ValueError(
                    "a survivor cannot have failing probes: " + ", ".join(self.probes_failed)
                )
            if self.plan.execution_path is not ExecutionPath.PRODUCTION_ENGINE:
                raise ValueError(
                    "a survivor must come from the production evaluation path; a standalone "
                    "study can be reproducible and still not be about this system"
                )
            if self.test_statistic is None:
                raise ValueError("a survivor must report the statistic it cleared the bar with")
            if abs(self.test_statistic) < self.plan.statistics.luck_threshold_z:
                raise ValueError(
                    f"a survivor must clear its own frozen bar: |{self.test_statistic}| < "
                    f"{self.plan.statistics.luck_threshold_z}"
                )
        return self

    @property
    def minimum_detectable_effect(self) -> Decimal:
        """Carried so a null reads "no effect larger than X" rather than "no effect found"."""
        return self.plan.statistics.minimum_detectable_effect

    @property
    def citable(self) -> bool:
        """Whether this may be cited as confirmatory evidence about Elrond."""
        return (
            self.verdict is OutcomeVerdict.SURVIVED
            and self.plan.execution_path is ExecutionPath.PRODUCTION_ENGINE
        )


__all__ = [
    "GENERALITY_PROBE",
    "REQUIRED_PROBES",
    "TEST_FOR",
    "CompilationRefused",
    "ExperimentOutcome",
    "ExperimentPlan",
    "OutcomeVerdict",
    "compile_experiment",
    "select_test",
]
