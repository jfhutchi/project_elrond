"""Whether the data can resolve the claim, and whether the claim is worth resolving (#19).

Two arithmetic questions asked before any compute is spent, both answerable from what the
registration declares and neither requiring the data to be touched:

1. **Can this sample detect this effect?** For a standardised effect `d` per observation the
   t-statistic is `d·sqrt(n)`, so clearing a bar `z` needs `n = (z/d)^2`. Every estimand here
   reduces to that, including Sharpe: an annualised Sharpe is a standardised mean difference
   with `d = SR / sqrt(observations per year)`. One core carries them, so they cannot drift
   apart.
2. **Would the effect survive its own costs?** An edge smaller than the spread it must cross is
   not a weak result, it is a negative one. `REFUTED.md` #8 is the paid-for version of this
   lesson: 1-day reversal produced the strongest raw edge ever measured in this project
   (+427.9% gross) and netted -28.8% at 5bps, never beating the slow signal at any cost level
   including an unachievable 0.5bps. That was decidable before the backtest ran.

**Variance inflation is not optional here.** `n` counts observations; the formulas need
*independent* observations, and this project's own strategies violate that badly. 2,669 daily
observations of a 252-day forward return are not 2,669 independent draws, they are closer to
ten, and treating them as independent overstates the t-statistic by roughly sqrt(252) -- in the
flattering direction, which is the direction every defect in this repository has taken. Three
declared sources are therefore applied and recorded:

* AR(1) autocorrelation of the estimand's series: `(1+rho)/(1-rho)`.
* Clustering of correlated units within a period: `1 + (m-1)*ICC`.
* Horizon overlap when an h-observation forward target is sampled every observation: `h`.

**No Monte Carlo, deliberately.** The gate runs before the data is read, which is the point of
running it. At that moment only declared parameters exist, so simulating a null from them
converges to the closed form with sampling noise added. The version that would add information
-- a block bootstrap under the null on the real series -- requires reading the window this gate
exists to protect. Power is dispatched by estimand, so an estimand with no analytic form is one
branch rather than a redesign; it is deferred until one is actually registered.
"""

from __future__ import annotations

import math
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

#: Every derived statistic is quantised before it is stored or hashed, so a registration's
#: content hash does not depend on float repr.
PRECISION = Decimal("0.000001")

#: Measured NBBO on 2026-08-17: SPY 0.26bps, QQQ 0.41, IWM 0.66, median ~1.1bps round trip.
#: Recorded as the floor a cost assumption should not silently sit below.
MEASURED_MEDIAN_ROUND_TRIP_BPS = Decimal("1.1")


class Estimand(StrEnum):
    """What the hypothesis is actually estimating. Fixes the power formula and the units."""

    #: Annualised Sharpe ratio against a null of zero.
    SHARPE = "SHARPE"
    #: Standardised mean difference (Cohen's d) per observation.
    MEAN_DIFFERENCE = "MEAN_DIFFERENCE"
    #: Top-minus-bottom portfolio spread, standardised per observation. Same arithmetic as a
    #: mean difference; named separately because the registration means something different.
    CROSS_SECTIONAL_SPREAD = "CROSS_SECTIONAL_SPREAD"
    #: Proportion correct against a declared null proportion.
    HIT_RATE = "HIT_RATE"
    #: Correlation between signal and forward return, against a null of zero.
    INFORMATION_COEFFICIENT = "INFORMATION_COEFFICIENT"


#: Estimands measured in return units, for which the cost floor is computable and required.
RETURN_LIKE = frozenset(
    {Estimand.SHARPE, Estimand.MEAN_DIFFERENCE, Estimand.CROSS_SECTIONAL_SPREAD}
)


class ComparisonStructure(StrEnum):
    """Dependence between the arms, which decides the test and the sample it needs.

    Cycle 11 used unpaired t-tests on paired data and the overnight premium looked significant
    when it is not. The correction dropped SPY from t=2.74 to t=0.63.
    """

    PAIRED = "PAIRED"
    UNPAIRED = "UNPAIRED"
    SINGLE_SAMPLE = "SINGLE_SAMPLE"


class Sampling(StrEnum):
    NON_OVERLAPPING = "NON_OVERLAPPING"
    OVERLAPPING = "OVERLAPPING"


class PowerVerdict(StrEnum):
    POWERED = "POWERED"
    #: The data cannot resolve the claim. Emphatically not `REFUTED`: recording it as refuted
    #: teaches a later agent that a mechanism was tested and failed when it was never tested.
    UNDERPOWERED = "UNDERPOWERED"
    #: The minimum effect worth acting on cannot pay its own trading costs.
    UNECONOMIC = "UNECONOMIC"
    #: Underpowered, and an operator authorised it anyway. Never becomes `POWERED`.
    OVERRIDDEN = "OVERRIDDEN"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def quantize(value: float | Decimal) -> Decimal:
    return Decimal(repr(float(value))).quantize(PRECISION, rounding=ROUND_HALF_EVEN)


def luck_threshold(trials: int) -> Decimal:
    """The t-statistic the best of `trials` skill-free candidates is expected to reach.

    `sqrt(2 ln N)`, the asymptotic maximum of N standard normals. It over-states the finite-N
    expectation, deliberately: in a project where every defect found so far flattered the
    result, a bar that errs high is the safe direction. Reproduces the table on issue #19 --
    2.52 / 3.03 / 3.72 / 4.29 at 24 / 100 / 1,000 / 10,000 trials.

    Recorded rather than reconciled: cycles 15-17 quote this bar (62 trials -> 2.87, 68 -> 2.90)
    but cycle 10 quotes 2.22 for 43 trials, which is Bailey & Lopez de Prado's finite-N expected
    maximum -- about 0.5 lower at comparable N. Bars quoted before cycle 15 are therefore not
    comparable with bars quoted after it, and neither number is retro-fitted here.
    """
    if trials < 2:
        # No selection happened, so there is nothing to deflate: plain two-sided 5%.
        return Decimal("1.96")
    return quantize(math.sqrt(2 * math.log(trials)))


class DependenceAssumptions(FrozenModel):
    """What was assumed to turn a count of observations into a count of independent ones.

    Recorded with every power number, because a minimum detectable effect quoted without them is
    false precision.

    **Overlap is a property of the target, never of the features** (#26). A 252-day forward return
    sampled daily really does reduce 2,669 observations to about 10; a 252-day *feature lookback*
    predicting the next session does not. The two are separate mechanisms and they are paid for in
    separate terms:

    * a reused **outcome** window is `horizon_observations` under `OVERLAPPING` sampling;
    * a persistent **signal** is `lag_one_autocorrelation`, because a slow feature produces
      serially correlated positions rather than overlapping outcomes.

    Putting a feature lookback in the overlap slot therefore double-counts one mechanism while
    leaving the other at zero. `feature_lookback_observations` is recorded so that mistake is
    auditable, and it is deliberately not an input to `variance_inflation()`.
    """

    #: AR(1) coefficient of the estimand's own series. Inflates variance by (1+rho)/(1-rho).
    #: This is where a slow-moving feature is paid for, not the overlap term.
    lag_one_autocorrelation: Decimal = Field(default=Decimal("0"), gt=-1, lt=1)
    #: Correlated units observed together, e.g. 23 ETFs ranked on the same day.
    cluster_size: int = Field(default=1, ge=1)
    intra_cluster_correlation: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    #: Whether an h-observation forward target is sampled every observation.
    sampling: Sampling = Sampling.NON_OVERLAPPING
    #: Length of the forward **target**, in observations. Only bites when sampling overlaps.
    #: Not the feature lookback -- see the class docstring.
    horizon_observations: int = Field(default=1, ge=1)
    #: How much history the signal consumes. Provenance only: it never enters the variance, and a
    #: test asserts that it cannot. Required when sampling overlaps, so that copying the lookback
    #: into the horizon is a visible recorded act rather than an invisible default.
    feature_lookback_observations: int | None = Field(default=None, ge=1)
    observations_per_year: int = Field(default=252, ge=1)

    @model_validator(mode="after")
    def validate_assumptions(self) -> Self:
        if self.sampling is Sampling.OVERLAPPING:
            if self.horizon_observations == 1:
                raise ValueError("a 1-observation horizon cannot overlap; state the horizon")
            if self.feature_lookback_observations is None:
                raise ValueError(
                    "overlapping sampling must state the feature lookback alongside the target "
                    "horizon, so the two are recorded as the separate quantities they are"
                )
        return self

    def variance_inflation(self) -> Decimal:
        """How many raw observations one independent observation costs."""
        rho = float(self.lag_one_autocorrelation)
        serial = (1 + rho) / (1 - rho)
        clustering = 1 + (self.cluster_size - 1) * float(self.intra_cluster_correlation)
        overlap = self.horizon_observations if self.sampling is Sampling.OVERLAPPING else 1
        return quantize(serial * clustering * overlap)

    def independent_observations(self, observations: int) -> Decimal:
        return quantize(observations / float(self.variance_inflation()))


class EconomicProfile(FrozenModel):
    """What the edge has to pay for before it is worth having.

    Required for return-like estimands and forbidden otherwise, rather than optional. A field
    that is silently ignored reads exactly like a check that ran.
    """

    #: Round trips per year implied by the strategy the hypothesis describes.
    annual_rebalances: int = Field(ge=0)
    #: Annualised volatility of the strategy, used to turn a Sharpe into a return.
    expected_annual_volatility_bps: int = Field(gt=0)
    #: All-in cost of one round trip, both sides, in basis points of traded notional.
    round_trip_cost_bps: Decimal = Field(ge=0, allow_inf_nan=False)

    def annual_cost_drag_bps(self) -> Decimal:
        return quantize(self.annual_rebalances * float(self.round_trip_cost_bps))


class EffectSpecification(FrozenModel):
    """The claimed effect, in its own units, with everything needed to price and size it."""

    estimand: Estimand
    #: In the estimand's units: annualised Sharpe, Cohen's d, a proportion, or a correlation.
    expected: Decimal = Field(allow_inf_nan=False)
    #: The smallest value that would be worth acting on. Must not exceed `expected`.
    minimum_practical: Decimal = Field(allow_inf_nan=False)
    #: 0 for everything except `HIT_RATE`, where the coin-flip null is 0.5.
    null_value: Decimal = Field(default=Decimal("0"), allow_inf_nan=False)
    justification: Text
    comparison: ComparisonStructure
    dependence: DependenceAssumptions = DependenceAssumptions()
    economics: EconomicProfile | None = None
    #: The benchmark's own annualised Sharpe. Required for a comparative SHARPE registration,
    #: forbidden otherwise. Jobson-Korkie-Memmel needs both Sharpes, not only the difference:
    #: the variance of a Sharpe difference depends on the levels as well as the gap.
    benchmark_sharpe: Decimal | None = Field(default=None, allow_inf_nan=False)
    #: Correlation between the candidate's returns and the benchmark's. Not defaulted, because a
    #: defaulted correlation is the same fail-open pattern as a defaulted sample size (#24), and
    #: it is the term that decides whether the comparison is cheap or impossible.
    benchmark_correlation: Decimal | None = Field(default=None, ge=-1, le=1)

    @model_validator(mode="after")
    def validate_effect(self) -> Self:
        if self.expected <= self.null_value:
            raise ValueError("the expected effect must exceed the null it is tested against")
        if self.minimum_practical <= self.null_value:
            raise ValueError("the minimum practical effect must exceed the null")
        if self.minimum_practical > self.expected:
            raise ValueError("expected effect cannot be below the minimum worth acting on")
        comparative = self.comparison is not ComparisonStructure.SINGLE_SAMPLE
        benchmark = (self.benchmark_sharpe, self.benchmark_correlation)
        if self.estimand is Estimand.SHARPE and comparative:
            # builder.py maps (SHARPE, PAIRED) to Jobson-Korkie-Memmel. Sizing it as a one-sample
            # Sharpe against zero made the gate and the compiler describe different experiments,
            # and on this project's own example hypothesis that overstated power 1,738-fold.
            if any(value is None for value in benchmark):
                raise ValueError(
                    "a comparative Sharpe registration is a Sharpe *difference*: declare the "
                    "benchmark's own Sharpe and its correlation with the candidate, because the "
                    "variance of the difference depends on both"
                )
        elif any(value is not None for value in benchmark):
            raise ValueError(
                "benchmark_sharpe and benchmark_correlation describe a Sharpe difference; they "
                "do not apply to this estimand or comparison structure"
            )
        if self.estimand is Estimand.HIT_RATE:
            if not Decimal("0") < self.null_value < Decimal("1"):
                raise ValueError("a hit-rate null must be a proportion strictly inside (0, 1)")
            if not self.null_value < self.expected < Decimal("1"):
                raise ValueError("a hit rate must be a proportion strictly inside (0, 1)")
        elif self.null_value != Decimal("0"):
            raise ValueError(f"{self.estimand.value} is tested against a null of zero")
        if self.estimand is Estimand.INFORMATION_COEFFICIENT and self.expected >= 1:
            raise ValueError("an information coefficient must be below 1")

        needs_economics = self.estimand in RETURN_LIKE
        if needs_economics and self.economics is None:
            raise ValueError(
                f"{self.estimand.value} is measured in return units, so it must declare the "
                "costs it has to cross"
            )
        if not needs_economics and self.economics is not None:
            raise ValueError(
                f"{self.estimand.value} carries no return mapping, so an economic profile "
                "could not be checked and must not be declared"
            )
        return self

    def annualised_sharpe_equivalent(self, effect: Decimal) -> Decimal:
        """Express a return-like effect as an annualised Sharpe, for the cost comparison."""
        if self.estimand is Estimand.SHARPE:
            return quantize(effect)
        return quantize(float(effect) * math.sqrt(self.dependence.observations_per_year))


def jobson_korkie_memmel_variance(specification: EffectSpecification) -> float:
    """Variance factor for the difference of two correlated Sharpe ratios, in annual units.

    Memmel's correction to Jobson-Korkie:

        Var(S_a - S_b) ~ (1/T) * [ 2(1 - rho) + 0.5*(S_a^2 + S_b^2 - 2*rho^2*S_a*S_b) ]

    with T in years and the Sharpes annualised. This returns the bracketed factor.

    `builder.py` already selects this test for a paired Sharpe comparison. The power gate sized
    the same registration as a one-sample Sharpe against zero, so the compiler and the gate
    described different experiments. On this project's own example -- SPY_SMA200 against SPY
    buy-and-hold, Sharpes 0.929 and 0.903, correlation 0.6531 -- the gate reported that 8.4 years
    sufficed for a comparison that needs roughly 14,671. A 1,738-fold overstatement.

    Note the `2(1 - rho)` term: at high correlation it collapses, and a comparison that looks
    hopeless between independent series can become cheap between near-identical ones. That is the
    whole reason the correlation cannot be defaulted.
    """
    if specification.benchmark_sharpe is None or specification.benchmark_correlation is None:
        raise ValueError("a Sharpe difference needs the benchmark's Sharpe and correlation")
    candidate = float(specification.expected) + float(specification.benchmark_sharpe)
    benchmark = float(specification.benchmark_sharpe)
    rho = float(specification.benchmark_correlation)
    return 2 * (1 - rho) + 0.5 * (candidate**2 + benchmark**2 - 2 * rho**2 * candidate * benchmark)


def _is_sharpe_difference(specification: EffectSpecification) -> bool:
    return (
        specification.estimand is Estimand.SHARPE
        and specification.comparison is not ComparisonStructure.SINGLE_SAMPLE
    )


def _standardised_effect(specification: EffectSpecification, effect: Decimal) -> float:
    """Per-observation standardised effect: the `d` in `t = d*sqrt(n)`."""
    estimand = specification.estimand
    if estimand is Estimand.SHARPE:
        return float(effect) / math.sqrt(specification.dependence.observations_per_year)
    if estimand in (Estimand.MEAN_DIFFERENCE, Estimand.CROSS_SECTIONAL_SPREAD):
        return float(effect)
    if estimand is Estimand.HIT_RATE:
        null = float(specification.null_value)
        return (float(effect) - null) / math.sqrt(null * (1 - null))
    # Fisher's transform: atanh(rho) has standard error 1/sqrt(n-3).
    return math.atanh(float(effect))


def _arm_factor(comparison: ComparisonStructure) -> float:
    """An unpaired two-sample comparison needs twice the observations per arm."""
    return 2.0 if comparison is ComparisonStructure.UNPAIRED else 1.0


def observations_required(specification: EffectSpecification, threshold: Decimal) -> int:
    """Raw observations needed for the expected effect to clear `threshold`.

    Sized on the expected effect, because that is what the registration predicts it will see.
    The minimum practical effect is what the cost floor is judged against instead.
    """
    dependence = specification.dependence
    if _is_sharpe_difference(specification):
        # Years, not sessions: JKM is expressed in annual units because the Sharpes are.
        years = (float(threshold) / float(specification.expected)) ** 2 * (
            jobson_korkie_memmel_variance(specification)
        )
        independent = years * dependence.observations_per_year
        return math.ceil(independent * float(dependence.variance_inflation()))
    standardised = _standardised_effect(specification, specification.expected)
    independent = (float(threshold) / standardised) ** 2 * _arm_factor(specification.comparison)
    if specification.estimand is Estimand.INFORMATION_COEFFICIENT:
        # Fisher's transform loses three degrees of freedom.
        independent += 3
    return math.ceil(independent * float(dependence.variance_inflation()))


def minimum_detectable_effect(
    specification: EffectSpecification, observations: int, threshold: Decimal
) -> Decimal:
    """The smallest effect this sample could separate from the null, in the estimand's units.

    Emitted with every registration so a null result reads "no effect larger than X" rather
    than the far weaker "no effect found".
    """
    if observations <= 0:
        raise ValueError("observations must be positive")
    dependence = specification.dependence
    independent = float(dependence.independent_observations(observations))
    independent /= _arm_factor(specification.comparison)
    estimand = specification.estimand

    if estimand is Estimand.INFORMATION_COEFFICIENT:
        if independent <= 3:
            raise ValueError("an information coefficient needs more than three observations")
        return quantize(math.tanh(float(threshold) / math.sqrt(independent - 3)))

    if _is_sharpe_difference(specification):
        years = independent / dependence.observations_per_year
        if years <= 0:
            raise ValueError("a Sharpe difference needs at least one year of observations")
        return quantize(
            float(threshold) * math.sqrt(jobson_korkie_memmel_variance(specification) / years)
        )
    standardised = float(threshold) / math.sqrt(independent)
    if estimand is Estimand.SHARPE:
        return quantize(standardised * math.sqrt(dependence.observations_per_year))
    if estimand is Estimand.HIT_RATE:
        null = float(specification.null_value)
        return quantize(null + standardised * math.sqrt(null * (1 - null)))
    return quantize(standardised)


class PowerOverride(FrozenModel):
    """An operator accepting an underpowered experiment, with their reason on the record."""

    authorized_by: Text
    reason: Text


class PowerAssessment(FrozenModel):
    """One power decision, durably recorded whether it passed or refused.

    Written at registration and again before every confirmatory run, because the bar moves as
    trials accumulate and a hypothesis powered when frozen can be underpowered when it runs.
    """

    hypothesis_id: Text
    version: int = Field(ge=1)
    #: `REGISTRATION` or `EXECUTION`, so the two can be told apart in research memory.
    stage: Text
    assessed_at: datetime
    verdict: PowerVerdict
    estimand: Estimand
    cumulative_trials: int
    luck_threshold_z: Decimal
    observations_required: int
    observations_available: int
    independent_observations: Decimal
    variance_inflation: Decimal
    minimum_detectable_effect: Decimal
    expected_effect: Decimal
    minimum_practical_effect: Decimal
    annual_cost_drag_bps: Decimal | None
    minimum_practical_return_bps: Decimal | None
    dependence: DependenceAssumptions
    override: PowerOverride | None = None

    @property
    def shortfall_observations(self) -> int:
        return max(0, self.observations_required - self.observations_available)

    @property
    def cleared(self) -> bool:
        return self.verdict in (PowerVerdict.POWERED, PowerVerdict.OVERRIDDEN)


def assess(
    *,
    hypothesis_id: str,
    version: int,
    stage: str,
    specification: EffectSpecification,
    observations_available: int,
    cumulative_trials: int,
    assessed_at: datetime,
    override: PowerOverride | None = None,
) -> PowerAssessment:
    """Decide whether this claim is measurable and worth measuring. Pure; persists nothing.

    Kept free of the session so a refusal can be recorded by the caller in its own transaction:
    the transaction that raised a refusal has already rolled back by the time anyone sees it.
    """
    threshold = luck_threshold(cumulative_trials)
    required = observations_required(specification, threshold)
    detectable = minimum_detectable_effect(specification, observations_available, threshold)

    drag: Decimal | None = None
    practical_return: Decimal | None = None
    if specification.economics is not None:
        drag = specification.economics.annual_cost_drag_bps()
        practical_return = quantize(
            float(specification.annualised_sharpe_equivalent(specification.minimum_practical))
            * specification.economics.expected_annual_volatility_bps
        )

    if practical_return is not None and drag is not None and practical_return <= drag:
        verdict = PowerVerdict.UNECONOMIC
    elif observations_available < required:
        verdict = PowerVerdict.OVERRIDDEN if override is not None else PowerVerdict.UNDERPOWERED
    else:
        verdict = PowerVerdict.POWERED

    return PowerAssessment(
        hypothesis_id=hypothesis_id,
        version=version,
        stage=stage,
        assessed_at=assessed_at,
        verdict=verdict,
        estimand=specification.estimand,
        cumulative_trials=cumulative_trials,
        luck_threshold_z=threshold,
        observations_required=required,
        observations_available=observations_available,
        independent_observations=specification.dependence.independent_observations(
            observations_available
        ),
        variance_inflation=specification.dependence.variance_inflation(),
        minimum_detectable_effect=detectable,
        expected_effect=specification.expected,
        minimum_practical_effect=specification.minimum_practical,
        annual_cost_drag_bps=drag,
        minimum_practical_return_bps=practical_return,
        dependence=specification.dependence,
        override=override,
    )


def explain(assessment: PowerAssessment) -> str:
    """A refusal in the units the hypothesis used, not in abstract sample counts."""
    if assessment.verdict is PowerVerdict.UNECONOMIC:
        return (
            f"the smallest {assessment.estimand.value} worth acting on is worth "
            f"{assessment.minimum_practical_return_bps}bps a year against "
            f"{assessment.annual_cost_drag_bps}bps of annual trading cost"
        )
    return (
        f"an expected {assessment.estimand.value} of {assessment.expected_effect} needs "
        f"{assessment.observations_required} observations to clear the "
        f"t={assessment.luck_threshold_z} bar for {assessment.cumulative_trials} cumulative "
        f"trials, and only {assessment.observations_available} are available "
        f"({assessment.independent_observations} independent after variance inflation of "
        f"{assessment.variance_inflation}x). The smallest effect this sample could detect is "
        f"{assessment.minimum_detectable_effect}"
    )
