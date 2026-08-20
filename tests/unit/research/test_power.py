"""The power arithmetic, checked against figures this project already published.

Each estimand is asserted against its own closed form rather than against the implementation,
so a change to the code that alters a number has to argue with statistics rather than with a
recorded expectation.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from quantbot.research.power import (
    ComparisonStructure,
    DependenceAssumptions,
    EconomicProfile,
    EffectSpecification,
    Estimand,
    PowerOverride,
    PowerVerdict,
    Sampling,
    assess,
    luck_threshold,
    minimum_detectable_effect,
    observations_required,
)

NOW = datetime(2026, 8, 18, 14, 30, tzinfo=UTC)

#: 2016-01-04 to 2026-08 of SIP daily data, the whole history this project has.
SIP_SESSIONS = 2669


def economics(**overrides: object) -> EconomicProfile:
    fields: dict[str, object] = {
        "annual_rebalances": 12,
        "expected_annual_volatility_bps": 1500,
        "round_trip_cost_bps": Decimal("1.1"),
    }
    fields.update(overrides)
    return EconomicProfile(**fields)


def sharpe_effect(**overrides: object) -> EffectSpecification:
    expected = overrides.pop("expected", Decimal("1.0"))
    assert isinstance(expected, Decimal)
    fields: dict[str, object] = {
        "estimand": Estimand.SHARPE,
        "expected": expected,
        # Halved by default so overriding the expected effect stays valid on its own.
        "minimum_practical": expected / 2,
        "justification": "Cycle 16 measured SPY_SMA200 at Sharpe 0.91 in-sample.",
        "comparison": ComparisonStructure.SINGLE_SAMPLE,
        "economics": economics(),
    }
    fields.update(overrides)
    return EffectSpecification(**fields)


def test_the_luck_bar_reproduces_the_table_on_issue_19() -> None:
    assert round(float(luck_threshold(24)), 2) == 2.52
    assert round(float(luck_threshold(100)), 2) == 3.03
    assert round(float(luck_threshold(1000)), 2) == 3.72
    assert round(float(luck_threshold(10000)), 2) == 4.29
    # And the bars cycles 15-17 quoted, which the registry must not quietly move.
    assert str(luck_threshold(62)) == "2.873024"
    assert str(luck_threshold(68)) == "2.904998"


def test_sharpe_power_reproduces_the_years_table_on_issue_19() -> None:
    expected_years = {"0.20": 210, "0.30": 93, "0.50": 34, "0.80": 13, "1.00": 8}
    for sharpe, years in expected_years.items():
        needed = observations_required(sharpe_effect(expected=Decimal(sharpe)), Decimal("2.9"))
        assert round(needed / 252) == years, sharpe

    # The 1.96 column of the same table.
    unselected = observations_required(sharpe_effect(expected=Decimal("1.0")), Decimal("1.96"))
    assert round(unselected / 252, 1) == 3.8


def test_the_whole_sip_history_cannot_separate_a_sharpe_below_about_0_9_from_zero() -> None:
    """`REFUTED.md`'s structural limit, as a number the gate enforces."""
    detectable = minimum_detectable_effect(sharpe_effect(), SIP_SESSIONS, Decimal("2.9"))
    assert Decimal("0.89") < detectable < Decimal("0.93")
    # SPY buy-and-hold scores 0.90 on this window: past this point a candidate has to beat
    # the market on risk-adjusted return merely to clear noise.


def test_each_estimand_uses_its_own_closed_form_not_the_sharpe_one() -> None:
    threshold = Decimal("2.9")
    z = float(threshold)

    # Standardised mean difference: n = (z/d)^2, with no annualisation.
    mean_difference = EffectSpecification(
        estimand=Estimand.MEAN_DIFFERENCE,
        expected=Decimal("0.2"),
        minimum_practical=Decimal("0.1"),
        justification="A tenth of a standard deviation per session.",
        comparison=ComparisonStructure.SINGLE_SAMPLE,
        economics=economics(),
    )
    assert observations_required(mean_difference, threshold) == math.ceil((z / 0.2) ** 2)

    # Hit rate against a coin flip: n = z^2 * p0(1-p0) / (p-p0)^2.
    hit_rate = EffectSpecification(
        estimand=Estimand.HIT_RATE,
        expected=Decimal("0.55"),
        minimum_practical=Decimal("0.52"),
        null_value=Decimal("0.5"),
        justification="A 55% directional hit rate.",
        comparison=ComparisonStructure.SINGLE_SAMPLE,
    )
    assert observations_required(hit_rate, threshold) == math.ceil((z / (0.05 / 0.5)) ** 2)
    assert minimum_detectable_effect(hit_rate, 10_000, threshold) > Decimal("0.5")

    # Information coefficient via Fisher's transform: n = 3 + (z/atanh(rho))^2.
    ic = EffectSpecification(
        estimand=Estimand.INFORMATION_COEFFICIENT,
        expected=Decimal("0.05"),
        minimum_practical=Decimal("0.02"),
        justification="A 0.05 IC is a strong published signal.",
        comparison=ComparisonStructure.SINGLE_SAMPLE,
    )
    assert observations_required(ic, threshold) == math.ceil(3 + (z / math.atanh(0.05)) ** 2)


def test_overlapping_horizons_are_charged_for_rather_than_counted_as_independent() -> None:
    """2,669 daily observations of a 252-day forward return are not 2,669 draws.

    This is the flattering error the gate exists to stop: treating them as independent
    understates the sample needed by the overlap factor.
    """
    independent = sharpe_effect()
    overlapping = sharpe_effect(
        dependence=DependenceAssumptions(
            sampling=Sampling.OVERLAPPING,
            # A genuine 252-session forward target, which is what overlap means. The feature
            # lookback is stated separately and must not affect the number (#26).
            horizon_observations=252,
            feature_lookback_observations=200,
        )
    )
    assert overlapping.dependence.variance_inflation() == Decimal("252")
    inflated = observations_required(overlapping, Decimal("2.9"))
    plain = observations_required(independent, Decimal("2.9"))
    assert 251.9 < inflated / plain < 252.1
    assert minimum_detectable_effect(
        overlapping, SIP_SESSIONS, Decimal("2.9")
    ) > minimum_detectable_effect(independent, SIP_SESSIONS, Decimal("2.9"))


def test_autocorrelation_and_clustering_inflate_the_sample_they_each_describe() -> None:
    serial = DependenceAssumptions(lag_one_autocorrelation=Decimal("0.5"))
    assert serial.variance_inflation() == Decimal("3")  # (1+0.5)/(1-0.5)

    clustered = DependenceAssumptions(cluster_size=23, intra_cluster_correlation=Decimal("0.5"))
    assert clustered.variance_inflation() == Decimal("12")  # 1 + 22*0.5

    assert DependenceAssumptions().variance_inflation() == Decimal("1")


def test_an_unpaired_comparison_needs_twice_the_observations_of_a_paired_one() -> None:
    """Cycle 11 used the wrong one and the overnight premium looked significant.

    Stated on `MEAN_DIFFERENCE`, which is where the arm factor genuinely applies. A comparative
    `SHARPE` no longer doubles: it is sized by Jobson-Korkie-Memmel, where the paired and unpaired
    cases differ through the correlation term rather than a factor of two, and that is asserted
    separately.
    """

    def difference(comparison: ComparisonStructure) -> EffectSpecification:
        return EffectSpecification(
            estimand=Estimand.MEAN_DIFFERENCE,
            # Small enough that the ceiling rounding is negligible against the ratio: at
            # d=0.2 the counts are 211 and 421, and 421/211 is 1.9953, not 2.
            expected=Decimal("0.02"),
            minimum_practical=Decimal("0.01"),
            justification="Two hundredths of a standard deviation per session.",
            comparison=comparison,
            economics=economics(),
        )

    paired = observations_required(difference(ComparisonStructure.PAIRED), Decimal("2.9"))
    unpaired = observations_required(difference(ComparisonStructure.UNPAIRED), Decimal("2.9"))
    assert 1.999 < unpaired / paired < 2.001


def test_an_edge_that_cannot_pay_its_costs_is_uneconomic_not_underpowered() -> None:
    """`REFUTED.md` #8: the strongest raw edge measured here netted -28.8% at 5bps.

    The sample is deliberately enormous, so the power gate cannot fire and only the cost
    floor can produce a refusal.
    """
    daily_churn = sharpe_effect(
        expected=Decimal("2.0"),
        minimum_practical=Decimal("0.2"),
        economics=economics(annual_rebalances=252, round_trip_cost_bps=Decimal("5")),
    )
    assessment = assess(
        hypothesis_id="H-COST",
        version=1,
        stage="REGISTRATION",
        specification=daily_churn,
        observations_available=1_000_000,
        cumulative_trials=68,
        assessed_at=NOW,
    )
    assert assessment.verdict is PowerVerdict.UNECONOMIC
    # 0.2 Sharpe on 1500bps of vol is 300bps a year against 252*5 = 1260bps of cost.
    assert assessment.minimum_practical_return_bps == Decimal("300")
    assert assessment.annual_cost_drag_bps == Decimal("1260")
    assert not assessment.cleared


def test_the_same_edge_traded_slowly_at_real_measured_costs_clears_the_floor() -> None:
    """The counterpart: the cost floor rejects the churn, not the edge."""
    assessment = assess(
        hypothesis_id="H-COST",
        version=1,
        stage="REGISTRATION",
        specification=sharpe_effect(expected=Decimal("2.0"), minimum_practical=Decimal("0.2")),
        observations_available=1_000_000,
        cumulative_trials=68,
        assessed_at=NOW,
    )
    assert assessment.verdict is PowerVerdict.POWERED


def test_a_return_like_estimand_must_declare_the_costs_it_has_to_cross() -> None:
    with pytest.raises(ValueError, match="costs it has to cross"):
        sharpe_effect(economics=None)


def test_an_estimand_with_no_return_mapping_may_not_declare_economics_it_cannot_check() -> None:
    with pytest.raises(ValueError, match="could not be checked"):
        EffectSpecification(
            estimand=Estimand.INFORMATION_COEFFICIENT,
            expected=Decimal("0.05"),
            minimum_practical=Decimal("0.02"),
            justification="An IC carries no return mapping on its own.",
            comparison=ComparisonStructure.SINGLE_SAMPLE,
            economics=economics(),
        )


def test_an_operator_override_is_recorded_and_never_becomes_powered() -> None:
    underpowered = sharpe_effect(expected=Decimal("0.3"))
    refused = assess(
        hypothesis_id="H-OVERRIDE",
        version=1,
        stage="REGISTRATION",
        specification=underpowered,
        observations_available=SIP_SESSIONS,
        cumulative_trials=68,
        assessed_at=NOW,
    )
    assert refused.verdict is PowerVerdict.UNDERPOWERED

    override = PowerOverride(authorized_by="hutch", reason="exploratory precursor, accepted")
    accepted = assess(
        hypothesis_id="H-OVERRIDE",
        version=1,
        stage="REGISTRATION",
        specification=underpowered,
        observations_available=SIP_SESSIONS,
        cumulative_trials=68,
        assessed_at=NOW,
        override=override,
    )
    assert accepted.verdict is PowerVerdict.OVERRIDDEN
    assert accepted.cleared
    assert accepted.override == override
    assert accepted.shortfall_observations == refused.shortfall_observations > 0


def test_an_override_cannot_rescue_an_uneconomic_claim() -> None:
    """Unmeasurable is a sampling problem; unprofitable is not, so an override does not apply."""
    assessment = assess(
        hypothesis_id="H-COST",
        version=1,
        stage="REGISTRATION",
        specification=sharpe_effect(
            expected=Decimal("2.0"),
            minimum_practical=Decimal("0.2"),
            economics=economics(annual_rebalances=252, round_trip_cost_bps=Decimal("5")),
        ),
        observations_available=1_000_000,
        cumulative_trials=68,
        assessed_at=NOW,
        override=PowerOverride(authorized_by="hutch", reason="run it anyway"),
    )
    assert assessment.verdict is PowerVerdict.UNECONOMIC
    assert not assessment.cleared


def test_an_overlapping_claim_must_state_the_horizon_it_overlaps() -> None:
    with pytest.raises(ValueError, match="state the horizon"):
        DependenceAssumptions(sampling=Sampling.OVERLAPPING, horizon_observations=1)


def test_statistical_power_follows_years_not_observation_count() -> None:
    """The correction that redirects this project's data strategy.

    For an annualised Sharpe the sampling frequency cancels: SE ~= sqrt((1+SR^2/2)/years), so
    to first order the detectable effect depends on the span in years, not the number of rows.
    A monthly series over 35 years therefore resolves a *smaller* effect than a daily series
    over 10.6, despite having a twentieth of the observations.

    This is asserted because the opposite assumption -- that low-frequency data is weak data --
    is easy to make and would have written off the only untouched data this project can get.
    """
    bar = Decimal("2.9")

    def annualised(observations: int, per_year: int, rho: str = "0") -> Decimal:
        return minimum_detectable_effect(
            sharpe_effect(
                dependence=DependenceAssumptions(
                    observations_per_year=per_year,
                    lag_one_autocorrelation=Decimal(rho),
                )
            ),
            observations,
            bar,
        )

    # Same span, wildly different row counts: the answer is the same.
    assert annualised(2520, 252) == annualised(120, 12)

    # More rows over a shorter span is *worse* than fewer rows over a longer one.
    ten_years_daily = annualised(2669, 252)
    thirty_five_years_monthly = annualised(420, 12)
    assert thirty_five_years_monthly < ten_years_daily
    assert ten_years_daily > Decimal("0.85")
    assert thirty_five_years_monthly < Decimal("0.50")

    # And the advantage survives the autocorrelation a macro series actually carries, which is
    # where an over-optimistic reading of the above would come unstuck.
    assert annualised(420, 12, rho="0.30") < annualised(2669, 252, rho="0.05")


def jkm_effect(**overrides: object) -> EffectSpecification:
    fields: dict[str, object] = {
        "estimand": Estimand.SHARPE,
        "expected": Decimal("0.55"),
        "minimum_practical": Decimal("0.25"),
        "justification": "A trend filter should improve risk-adjusted return over buy-and-hold.",
        "comparison": ComparisonStructure.PAIRED,
        "benchmark_sharpe": Decimal("0.90"),
        "benchmark_correlation": Decimal("0.95"),
        "economics": economics(),
    }
    fields.update(overrides)
    return EffectSpecification(**fields)


def test_a_comparative_sharpe_cannot_be_registered_without_its_benchmark() -> None:
    """The #25 defect made constructible-proof.

    `builder.py` maps (SHARPE, PAIRED) to Jobson-Korkie-Memmel, whose variance depends on both
    Sharpes and their correlation. The gate sized the same registration as a one-sample Sharpe
    against zero, so the compiler and the gate described different experiments. A spec that omits
    the benchmark can no longer be built, which is what makes the two agree by construction rather
    than by discipline.
    """
    with pytest.raises(ValidationError, match="Sharpe \*difference\*"):
        jkm_effect(benchmark_sharpe=None, benchmark_correlation=None)
    with pytest.raises(ValidationError, match="Sharpe \*difference\*"):
        jkm_effect(benchmark_correlation=None)


def test_a_standalone_sharpe_may_not_carry_a_benchmark() -> None:
    """The mismatch is refused in both directions, so the fields cannot become decorative."""
    with pytest.raises(ValidationError, match="do not apply"):
        jkm_effect(comparison=ComparisonStructure.SINGLE_SAMPLE)


def test_the_absolute_sharpe_gate_overstated_the_projects_own_example_1738_fold() -> None:
    """Measured on SPY_SMA200 vs SPY buy-and-hold, SIP 2016-2026, 2,667 sessions.

    Sharpes 0.929 and 0.903, correlation 0.6531, difference +0.026. The old gate was fed the
    *absolute* Sharpe of 1.0 for a question whose registered estimand was the difference, and
    reported that 8.4 years sufficed. Under the test the compiler actually selects, the same
    comparison needs roughly 14,671 years.

    This asserts the direction and the order of magnitude rather than a fragile exact figure: the
    claim under test is that the comparative sizing is enormously larger, not that it equals any
    particular constant.
    """
    threshold = luck_threshold(68)
    absolute = observations_required(
        EffectSpecification(
            estimand=Estimand.SHARPE,
            expected=Decimal("1.0"),
            minimum_practical=Decimal("0.5"),
            justification="the standalone Sharpe the old gate was fed",
            comparison=ComparisonStructure.SINGLE_SAMPLE,
            economics=economics(),
        ),
        threshold,
    )
    comparative = observations_required(
        jkm_effect(
            expected=Decimal("0.026"),
            minimum_practical=Decimal("0.01"),
            benchmark_sharpe=Decimal("0.903"),
            benchmark_correlation=Decimal("0.6531"),
        ),
        threshold,
    )
    assert absolute / 252 < 10, "the old sizing cleared inside the available history"
    assert comparative / 252 > 10_000, "the real comparison is not resolvable by this project"
    assert comparative / absolute > 1_000


def test_correlation_decides_whether_a_sharpe_comparison_is_cheap_or_impossible() -> None:
    """The `2(1 - rho)` term, which is why the correlation is not allowed a default.

    Near-identical strategies are *easy* to separate on Sharpe and independent ones are hard, so a
    defaulted correlation would silently pick a verdict rather than measure one — the same
    fail-open shape as the defaulted sample size in #24.
    """
    threshold = luck_threshold(68)
    near_identical = observations_required(
        jkm_effect(benchmark_correlation=Decimal("0.99")), threshold
    )
    independent = observations_required(jkm_effect(benchmark_correlation=Decimal("0")), threshold)
    assert near_identical < independent
    assert independent / near_identical > 3


def test_the_detectable_sharpe_difference_shrinks_with_correlation_too() -> None:
    """`minimum_detectable_effect` must use the same variance as `observations_required`.

    They were separate code paths before, and a null result reported through the one-sample
    formula would understate what the experiment had actually ruled out.
    """
    sessions = 2669
    threshold = luck_threshold(68)
    tight = minimum_detectable_effect(
        jkm_effect(benchmark_correlation=Decimal("0.99")), sessions, threshold
    )
    loose = minimum_detectable_effect(
        jkm_effect(benchmark_correlation=Decimal("0")), sessions, threshold
    )
    assert tight < loose


def test_feature_lookback_cannot_change_the_variance_no_matter_what_it_is() -> None:
    """`#26`: overlap is a property of the target, never of the features.

    A 252-day forward return sampled daily really does reduce 2,669 observations to about 10. A
    252-day feature *lookback* predicting the next session does not, and the module docstring
    previously used the second while quoting the arithmetic of the first.

    Provenance that is only documented as unused eventually gets used. This is the assertion that
    keeps it true: the field is recorded and it cannot move the number.
    """
    base = DependenceAssumptions(
        sampling=Sampling.OVERLAPPING,
        horizon_observations=21,
        feature_lookback_observations=1,
    )
    for lookback in (1, 50, 200, 252, 5000):
        varied = base.model_copy(update={"feature_lookback_observations": lookback})
        assert varied.variance_inflation() == base.variance_inflation()
        assert varied.independent_observations(2669) == base.independent_observations(2669)


def test_the_target_horizon_does_change_the_variance() -> None:
    """The other half of the pair, so the test above cannot pass by everything being inert."""

    def assumptions(horizon: int) -> DependenceAssumptions:
        return DependenceAssumptions(
            sampling=Sampling.OVERLAPPING,
            horizon_observations=horizon,
            feature_lookback_observations=200,
        )

    assert assumptions(252).variance_inflation() == Decimal("252")
    assert assumptions(21).variance_inflation() == Decimal("21")
    assert assumptions(252).independent_observations(2669) < Decimal("11")


def test_a_slow_feature_is_paid_for_through_autocorrelation_not_overlap() -> None:
    """Where a 200-day lookback legitimately costs something: serially correlated positions.

    Stating this as a test rather than a comment because the two mechanisms were merged for long
    enough to reach the documentation, and the distinction is the whole content of #26.
    """
    persistent_signal = DependenceAssumptions(lag_one_autocorrelation=Decimal("0.98"))
    overlapping_target = DependenceAssumptions(
        sampling=Sampling.OVERLAPPING,
        horizon_observations=99,
        feature_lookback_observations=1,
    )
    # (1 + 0.98)/(1 - 0.98) = 99, reached with no overlap at all.
    assert persistent_signal.variance_inflation() == Decimal("99")
    assert overlapping_target.variance_inflation() == Decimal("99")
    assert persistent_signal.sampling is Sampling.NON_OVERLAPPING


def test_overlapping_sampling_must_state_both_quantities() -> None:
    """Copying the lookback into the horizon becomes a recorded act rather than a default."""
    with pytest.raises(ValidationError, match="feature lookback"):
        DependenceAssumptions(sampling=Sampling.OVERLAPPING, horizon_observations=252)
