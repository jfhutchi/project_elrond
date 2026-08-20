# Research Cycle 4 — Pre-Registered

Written before any cycle-4 result was observed.

## Standing on

Cycle 3 found that correlation structure, not signal design, determines whether combining
strategies helps: four signal families sharing equity beta correlated 0.44–0.75 and did not
diversify, while the same signal confined to asset-class sleeves correlated 0.05–0.22 and
lifted the ensemble from Sharpe 0.82 / 25.29% drawdown to 0.93 / 20.84%.

That result is **provisional**: the sleeves were designed after the correlation failure was
observed, so it is iteration on seen data. Cycle 4 exists to test it properly.

## Hypotheses

**H1 (primary).** An ensemble of asset-class sleeves — each running an identical, untuned
12-1 absolute-momentum rule — achieves a higher risk-adjusted return than any single sleeve,
and than the shipped strategy, because its components carry different risk factors rather
than different signals.

**H2.** The diversification benefit is driven by the bond sleeve specifically, being the
only component correlating below 0.25 with equities. Removing it should cost more Sharpe
than removing any equally-weighted alternative, despite it being the weakest standalone
component.

**H3 (null, and the outcome published evidence favours).** After production-engine costs and
fills, the ensemble's advantage over a simple SPY buy-and-hold disappears on risk-adjusted
terms, as it did in cycles 1 and 2.

## Fixed design — no component may be added after results are seen

Three sleeves, one shared rule, no per-sleeve tuning:

| Sleeve | Universe | Hold |
|---|---|---|
| equity | SPY QQQ IWM MDY EFA EEM XLB XLE XLF XLI XLK XLP XLU XLV XLY | 3 |
| bonds | TLT IEF TIP LQD HYG | 2 |
| real assets | DBC GLD VNQ | 1 |

Rule, identical in every sleeve: rank by 12-1 momentum; hold only positive-momentum names;
equal weight; monthly rebalance; hold cash when nothing qualifies.

Weighting between sleeves: **equal weight, fixed in advance.** No optimisation of sleeve
weights is permitted in this cycle — optimising three weights against one history is exactly
the search that produced cycles 1 and 2's failures.

## Selection rule, fixed in advance

There is nothing to select. The design is fully specified above, so cycle 4 is a single
confirmation run, not a search. This is deliberate: it makes the result interpretable
without a multiple-testing correction, because the number of trials is one.

## Promotion criteria, fixed in advance

Promotion to a new paper strategy version requires **all** of:

1. Confirmed in the production Decimal engine, not the simplified simulator.
2. Ensemble Sharpe exceeds every individual sleeve's Sharpe over the full period.
3. Ensemble maximum drawdown is below the equity sleeve's.
4. Ensemble Sharpe's 95% confidence interval excludes zero.
5. Ensemble Sharpe is not below SPY buy-and-hold's over the same period.

Criterion 5 is the one cycles 1 and 2 failed. If it fails again the honest conclusion is
that this strategy family does not beat holding the benchmark, and that should be reported
rather than researched around.

## Acknowledged limitation, stated before results

**No untouched historical holdout remains.** The 2024-07 → 2026-08 window was consumed in
cycle 2. Cycle 4 therefore cannot produce an out-of-sample verdict from history at all — it
can only confirm or refute the design in-sample under fixed rules.

The only genuinely clean out-of-sample data available to this project from here is the live
paper account, going forward in real time. Any cycle-4 result is a hypothesis awaiting that
test, never a validated edge.

## Enabling work required first

`StrategyConfig` requires SPY in every universe and pins `benchmark_symbol` and
`regime_symbol` to SPY within it, so a bond-only sleeve cannot be expressed. The regime
symbol must be separable from the tradeable universe before criterion 1 can be met. That is
a new strategy version, not an edit to an existing one.
