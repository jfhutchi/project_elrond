"""Where statistical capacity actually exists, before any of it is spent.

`CLAUDE.md`: a cheap failed power analysis is preferable to an expensive meaningless backtest.
This is the cheap analysis, run *before* pulling a dataset rather than after failing to resolve
anything with it.

The result corrects an assumption that is easy to make and that I made in writing:
**low-frequency data is not weak data.** For an annualised Sharpe the sampling frequency
cancels out. The standard error of an annualised Sharpe is approximately

    SE ~= sqrt((1 + SR_p^2 / 2) / years)

so to first order the detectable effect depends on the **span in years**, not on the number of
observations. A monthly series covering 35 years resolves a smaller effect than a daily series
covering 10.6, despite having a twentieth of the rows.

That matters here because the equity window is exhausted at 10.6 years, and the macro series
that are free to obtain run three to seven times longer.

Dependence is where the advantage is given back, and it is charged rather than ignored. Macro
series and strategies driven by them are materially more autocorrelated than daily equity
returns, so the survey reports both the iid figure and a realistic AR(1) one. The honest number
is the second.

Usage:
    uv run python scripts/power_survey.py
"""

from __future__ import annotations

from decimal import Decimal

from quantbot.research.power import (
    ComparisonStructure,
    DependenceAssumptions,
    EconomicProfile,
    EffectSpecification,
    Estimand,
    luck_threshold,
    minimum_detectable_effect,
)
from quantbot.research.registry import PRIOR_TRIALS

#: Placeholder economics. The cost floor is not what this survey is about, and the values do
#: not enter the minimum detectable effect at all.
ECONOMICS = EconomicProfile(
    annual_rebalances=12,
    expected_annual_volatility_bps=1500,
    round_trip_cost_bps=Decimal("1.1"),
)


def detectable(
    *, observations: int, per_year: int, autocorrelation: str
) -> tuple[Decimal, Decimal]:
    """Minimum detectable annualised Sharpe, and the variance inflation charged for it."""
    specification = EffectSpecification(
        estimand=Estimand.SHARPE,
        expected=Decimal("1"),
        minimum_practical=Decimal("0.5"),
        justification="survey placeholder; the expected effect does not enter the MDE",
        comparison=ComparisonStructure.PAIRED,
        dependence=DependenceAssumptions(
            observations_per_year=per_year,
            lag_one_autocorrelation=Decimal(autocorrelation),
        ),
        economics=ECONOMICS,
    )
    bar = luck_threshold(PRIOR_TRIALS)
    return (
        minimum_detectable_effect(specification, observations, bar),
        specification.dependence.variance_inflation(),
    )


#: Datasets worth comparing. Spans are the real ones: SIP begins 2016-01-04, FRED's CPI series
#: begins 1947, and the 10y-2y spread begins 1976.
CANDIDATES: tuple[tuple[str, int, int, str], ...] = (
    ("SIP US equities, daily, 10.6y  [EXHAUSTED]", 2669, 252, "0.05"),
    ("FRED monthly, 35y (PAYEMS, UNRATE)", 420, 12, "0.30"),
    ("FRED monthly, 75y (CPIAUCSL from 1947)", 900, 12, "0.30"),
    ("FRED daily, 50y (T10Y2Y from 1976)", 12_500, 252, "0.30"),
)


def main() -> int:
    bar = luck_threshold(PRIOR_TRIALS)
    print(f"cumulative trials {PRIOR_TRIALS}, luck bar t={bar}")
    print()
    print(f"{'dataset':44}{'years':>7}{'rho':>6}{'infl':>7}{'MDE Sharpe':>12}")
    print("-" * 76)
    for label, observations, per_year, rho in CANDIDATES:
        mde, inflation = detectable(
            observations=observations, per_year=per_year, autocorrelation=rho
        )
        years = observations / per_year
        print(f"{label:44}{years:7.1f}{rho:>6}{float(inflation):6.2f}x{mde:>12}")

    print()
    print("Read this as: an effect smaller than the MDE column cannot be distinguished from")
    print("zero on that dataset, however it is analysed. SPY buy-and-hold scores 0.90 on the")
    print("equity window -- i.e. sitting at its own detection limit, which is why nothing")
    print("measured there could ever separate from noise.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
