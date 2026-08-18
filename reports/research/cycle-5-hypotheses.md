# Research Cycle 5 — Exposure Normalisation (Pre-Registered)

Written before any cycle-5 result was observed.

## The open question this closes

Cycle 4 failed on one criterion only: ensemble Sharpe 0.82 against SPY buy-and-hold's 0.90.
Its sleeves ran far below full investment, because `max_position_value_bps` caps any single
holding at 10% of equity while a sleeve holds only one to three names. An equity sleeve
holding three names deploys at most 30% of capital; the rest sits in cash.

I noted at the time that this "explains most of the return gap" and refused to act on it,
because adjusting a parameter after seeing a failed criterion is the post-hoc rescue this
process exists to prevent. Cycle 5 tests it as a declared hypothesis instead, so the answer
is interpretable either way.

## Hypotheses

**H1.** Raising each sleeve's position cap to full investment (10000 bps divided by the
number of holdings) materially raises total return, because the same signal deploys more
capital.

**H2 (primary, and the reason this cycle is decisive).** It does **not** materially change
Sharpe. Holding a fraction *f* of capital in assets and the remainder in cash earning zero
produces returns of *f* × asset returns, so mean and standard deviation scale together and
the ratio is invariant. If H2 holds, cycle 4's criterion-5 failure is structural: **no
exposure adjustment can make this strategy family beat the benchmark on risk-adjusted
return**, and that avenue is closed permanently rather than left open.

**H3.** Maximum drawdown scales up roughly in proportion to exposure, so the ensemble's
3.93% drawdown advantage over SPY's 33.79% shrinks toward the benchmark as the return gap
closes — meaning the apparent safety was never skill, only cash.

## Fixed design

Identical to cycle 4 in every respect except the position cap. Same three sleeves, same
universes, same hold counts, same 12-1 absolute-momentum rule, same equal weighting, same
component switches, same data.

| Sleeve | Holds | `max_position_value_bps` |
|---|---:|---:|
| equity | 3 | 3333 |
| bonds | 2 | 5000 |
| real assets | 1 | 10000 |

No other parameter may be varied. One configuration, one run: the number of trials is one.

## Promotion criteria — unchanged from cycle 4, deliberately

1. Confirmed in the production Decimal engine.
2. Ensemble Sharpe exceeds every individual sleeve's Sharpe.
3. Ensemble maximum drawdown below the equity sleeve's.
4. Ensemble Sharpe's 95% confidence interval excludes zero.
5. Ensemble Sharpe is not below SPY buy-and-hold's.

Reusing cycle 4's criteria unchanged is the point. If exposure were the missing ingredient,
criterion 5 would now pass. If H2 is right it will fail by almost exactly the same margin,
which is the informative outcome.

## What each result would mean

- **Criterion 5 passes** → exposure was the binding constraint; a candidate exists and
  proceeds to human review.
- **Criterion 5 fails with Sharpe roughly unchanged** → H2 confirmed. The family's
  risk-adjusted shortfall is structural, not a sizing artifact. Recommendation: stop
  developing this strategy family.
- **Criterion 5 fails with Sharpe materially *worse*** → concentration is actively harmful
  at this universe size, and the 10% cap was doing useful work.
