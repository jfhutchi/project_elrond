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

from quantbot.research.artifact_testing import ArtifactOutcome, ArtifactTestRegistration
from quantbot.research.manifest import (
    VALID_FOR,
    DatasetSnapshot,
    ExecutionPath,
    StatisticalPlan,
    StatisticalTest,
)
from quantbot.research.power import (
    ComparisonStructure,
    Estimand,
    PowerVerdict,
    supports_registered_direction,
)
from quantbot.research.registry import Registration
from quantbot.research.validation_campaign import CampaignOutcome, CampaignVerdict

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
    #: Exact component/interaction graph frozen inside the registration, when componentized.
    hypothesis_graph_hash: Text | None = Field(default=None, min_length=64, max_length=64)
    statistics: StatisticalPlan
    datasets: tuple[DatasetSnapshot, ...] = Field(min_length=1)
    costs: dict[str, str]
    probes: tuple[Text, ...] = Field(min_length=1)
    #: A confirmatory result has to come from the engine that will actually trade.
    execution_path: ExecutionPath
    #: Separately preregistered, powered comparisons that may support artifact-level evidence.
    artifact_tests: tuple[ArtifactTestRegistration, ...] = ()
    #: The hostile-validation campaign the registration froze, if any (#159).
    #:
    #: Carried so an outcome can be checked against what the registration *required*, not only
    #: against what the outcome happens to carry. Without it, `ExperimentOutcome` had no way to
    #: tell "this claim registered no campaign" from "this claim registered one and the outcome
    #: omitted it", so a directly constructed SURVIVED outcome with `validation_outcome=None`
    #: walked past the gate by declining to mention it. A gate you pass by omission is not a
    #: gate.
    #:
    #: A plain optional field rather than #216's omit-when-absent serializer, because the
    #: compatibility constraint that forced that treatment does not exist here. `HypothesisDraft`
    #: is content-addressed and stored in `document_json`, so a new key moved every historical
    #: registration hash. `ExperimentPlan` is compiled per run, never persisted, never hashed and
    #: never embedded in a manifest -- `ExperimentManifest` records the registration hash and the
    #: snapshots, not the plan -- so nothing downstream can observe the added key. Verified by
    #: grep before adding it rather than assumed.
    validation_campaign_id: Text | None = Field(default=None, min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        missing = [probe for probe in REQUIRED_PROBES if probe not in self.probes]
        if missing:
            raise ValueError(f"a compiled experiment must run {', '.join(missing)}")
        test_ids = [test.test_id for test in self.artifact_tests]
        if len(test_ids) != len(set(test_ids)):
            raise ValueError("an experiment plan cannot contain the same artifact test twice")
        return self


def compile_experiment(
    registration: Registration,
    *,
    datasets: Sequence[DatasetSnapshot],
    execution_path: ExecutionPath = ExecutionPath.PRODUCTION_ENGINE,
    path_dependent: bool = False,
    artifact_tests: Sequence[ArtifactTestRegistration] = (),
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

    graph_hash = None if draft.graph is None else draft.graph.graph_id
    for artifact_test in artifact_tests:
        if (
            artifact_test.hypothesis_id != draft.hypothesis_id
            or artifact_test.hypothesis_version != draft.version
            or artifact_test.registration_hash != registration.content_hash
            or artifact_test.graph_hash != graph_hash
        ):
            raise CompilationRefused(
                "ARTIFACT_TEST_MISMATCHED",
                f"artifact test {artifact_test.test_id} was not frozen against "
                f"{draft.hypothesis_id} v{draft.version}@{registration.content_hash}",
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
        hypothesis_graph_hash=graph_hash,
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
        artifact_tests=tuple(artifact_tests),
        # Read from the registration, so a plan cannot decline to require the campaign its own
        # claim was frozen with. `None` is the honest value only when the draft froze none.
        validation_campaign_id=(
            None if draft.validation_campaign is None else draft.validation_campaign.campaign_id
        ),
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
    artifact_outcomes: tuple[ArtifactOutcome, ...] = ()
    #: The hostile-validation campaign frozen with the registration, graded by
    #: `validation_campaign.grade_campaign` (#159). `None` when the registration froze no
    #: campaign, which is every registration made before #159 and every one that declines to
    #: declare scenarios. It is an *additional* gate: it can block a survivor and can never
    #: manufacture one, so it is recorded beside the primary verdict rather than folded into it.
    validation_outcome: CampaignOutcome | None = None
    detail: Text
    completed_at: datetime

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        # Campaign identity first, and for every verdict rather than only for survivors. A
        # FAILED or INCONCLUSIVE outcome that names the wrong campaign, or silently drops the
        # one its plan required, is still a misrecorded run -- and the FAILED path is exactly
        # where a missing campaign would be least likely to be noticed.
        self._validate_campaign_binding()

        # FAILED did not complete; INCONCLUSIVE completed and is explicitly not evidence. Both
        # may carry an incomplete probe set, and the second exemption matters: without it a run
        # that finished but dropped a probe had to be recorded as FAILED, which says the
        # experiment never ran. That is a less accurate record, not a safer one.
        #
        # SURVIVED and REFUTED are evidence claims and stay barred, which is the whole
        # protection -- an unprobed result cannot be cited either way.
        if self.verdict in (OutcomeVerdict.FAILED, OutcomeVerdict.INCONCLUSIVE):
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
            # Signed. This validator is the structural backstop behind `runner._grade`: even if
            # a caller constructs an `ExperimentOutcome` directly, a SURVIVED verdict cannot
            # carry a statistic that fails its own one-sided bar. It read `abs(...)`, so it
            # would have certified a t of -9 against a claim of "greater than null" -- the same
            # inversion as the grader, in the type that exists to make the grader unnecessary.
            # A survivor cannot carry a campaign that refused it. `runner._grade` downgrades on
            # a blocking campaign, and this is the structural backstop behind that: a caller
            # constructing the outcome directly must not be able to keep SURVIVED while
            # attaching the record of the gate that objected. Without it the hostile-validation
            # result becomes decoration on a verdict it was supposed to control.
            if (
                self.validation_outcome is not None
                and self.validation_outcome.verdict is not CampaignVerdict.SURVIVED
            ):
                raise ValueError(
                    "a survivor cannot carry a validation campaign that did not survive: "
                    f"campaign {self.validation_outcome.campaign_id} is "
                    f"{self.validation_outcome.verdict.value}"
                )
            if not supports_registered_direction(
                self.test_statistic, self.plan.statistics.luck_threshold_z
            ):
                raise ValueError(
                    f"a survivor must clear its own frozen bar in the registered direction: "
                    f"{self.test_statistic} < {self.plan.statistics.luck_threshold_z}"
                )
        return self

    def _validate_campaign_binding(self) -> None:
        """Hold the outcome to the campaign the *plan* required, not the one it volunteered.

        The previous rule only refused a survivor carrying a campaign that objected, which
        leaves the cheaper route open: carry no campaign at all. `plan.validation_campaign_id`
        is what closes it -- the plan states whether a campaign is required, and the outcome is
        checked against that statement rather than against its own contents.

        Both directions are refused. A plan that required no campaign cannot acquire a
        validation outcome, because a campaign nobody registered is not hostile validation; and
        a plan that required campaign A cannot be satisfied by campaign B, because a favourable
        result under a campaign chosen after the fact is the search this whole slice exists to
        make expensive.
        """
        required = self.plan.validation_campaign_id
        campaign = self.validation_outcome
        if required is None:
            if campaign is not None:
                raise ValueError(
                    f"this experiment registered no validation campaign, so outcome "
                    f"{campaign.campaign_id} is not hostile validation of it"
                )
            return
        if campaign is None:
            raise ValueError(
                f"the registration froze validation campaign {required} and this outcome "
                "carries none; a gate passed by omitting it is not a gate"
            )
        if campaign.campaign_id != required:
            raise ValueError(
                f"the registration froze validation campaign {required} and this outcome "
                f"carries {campaign.campaign_id}; a result graded under a different campaign "
                "is a result about a different claim"
            )

    @property
    def minimum_detectable_effect(self) -> Decimal:
        """Carried so a null reads "no effect larger than X" rather than "no effect found"."""
        return self.plan.statistics.minimum_detectable_effect

    @property
    def citable(self) -> bool:
        """Whether this may be cited as confirmatory evidence about Elrond.

        Conservative about the campaign as well as the verdict, and it states the whole
        campaign rule itself rather than the half the validator makes convenient. The validator
        above already refuses every combination refused here, so all of this is redundant today
        -- and it is written out anyway, because `citable` is what a reader consults to decide
        whether a result may be quoted, and a rule that is only correct while some other rule
        holds is one refactor away from quietly citing an ungated survivor.
        """
        required = self.plan.validation_campaign_id
        campaign = self.validation_outcome
        campaign_supports = (
            campaign is None
            if required is None
            else (
                campaign is not None
                and campaign.campaign_id == required
                and campaign.verdict is CampaignVerdict.SURVIVED
            )
        )
        return (
            self.verdict is OutcomeVerdict.SURVIVED
            and self.plan.execution_path is ExecutionPath.PRODUCTION_ENGINE
            and campaign_supports
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
