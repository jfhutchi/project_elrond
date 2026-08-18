# Cycle 12 — Pre-registration

Written before measurement. Cumulative trials entering this cycle: ~58.

## Why this cycle exists

Cycle 11 established four survivors, none of them alpha. They improve risk, cost and mechanics.
None of them answers the question that decides whether a small account ever becomes a large
one: **what growth rate is achievable, and what limits it?**

Compounding is the only mechanism this project has confirmed works (REFUTED.md, "More capital"
and "More time"). The growth rate of a levered strategy is not linear in leverage — it rises,
peaks, then falls, because variance drag grows as the square of exposure. Cycle 6 tested fixed
2x and 3x and found them worse. It never located the peak, and it never charged margin
interest, which is the single largest omission: **borrowed money is not free, and at a $100
account the borrowing rate is high relative to the expected edge.**

## Hypotheses

### H12.1 — There is a growth-optimal leverage, and it is below 2x (Class A)
**Claim.** Long-run log growth is g = μL − σ²L²/2 − r(L−1), where r is the margin rate. This is
arithmetic, not a fitted effect. The optimum L* = (μ − r)/σ².
**Test.** Locate the empirical peak for SPY buy-and-hold, SPY 200d trend, vol-targeted SPY, and
the deployed momentum rotation, charging a real Alpaca margin rate.
**Falsified if.** The empirical optimum is not interior, or terminal wealth does not fall either
side of it.

### H12.2 — Margin interest destroys the leverage case at retail rates (Class A)
**Claim.** Cycle 6's leverage results assumed free borrowing. At a realistic retail margin rate
the optimum moves sharply toward 1x and may fall below it.
**Falsified if.** Charging interest moves the optimal leverage by less than 0.25x.

### H12.3 — Higher Sharpe supports more leverage, so Sharpe is the wealth variable (Class A)
**Claim.** L* scales with μ/σ², so a strategy with a modestly higher Sharpe can be levered
further before variance drag bites, and can therefore produce more terminal wealth even if its
unlevered return is lower. This is the only argument by which anything in this project beats
SPY on wealth, and it has never been tested.
**Falsified if.** At each strategy's own optimal leverage, none produces more terminal wealth
than SPY at SPY's optimal leverage.

### H12.4 — The drawdown constraint binds before the growth optimum (Class A)
**Claim.** The growth-optimal leverage implies drawdowns far past the project's 20% halt, so
the halt — not the mathematics — is the true constraint on growth.
**Falsified if.** The optimum's implied max drawdown is under 20%.

## Correction

Cumulative trials ~62 after this cycle; luck bar z > 2.87. These are Class A hypotheses whose
mechanism is arithmetic, so the measurement quantifies magnitude rather than discovering an
effect — but any *comparison between strategies* is a selection and is corrected as such.
