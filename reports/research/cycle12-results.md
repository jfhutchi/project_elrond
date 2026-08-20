# Cycle 12 — Results

Run 2026-08-18. Pre-registration: `cycle12-preregistration.md`.
Scripts: `cycle12_growth.py`, `cycle12_margincall.py`.

## Headline

**The 20% drawdown halt — not the signal — is what limits wealth in this project.**

SPY buy-and-hold has a 33.8% max drawdown. The project's own risk rule halts at 20%. So the
rule forbids holding the very benchmark that has beaten everything built here. Every strategy
must be sized down until it fits, and that sizing decision dominates every signal question
cycles 1–11 asked.

Given that constraint, one thing genuinely helps, and it is the cycle 11 survivor reframed.

## Answered: what actually maximises wealth under the 20% rule

| strategy | max leverage under 20% DD | $100 becomes |
|---|---|---|
| SPY buy-and-hold | 0.50x | $281.02 |
| SPY 200d trend | 1.00x | $311.01 |
| **SPY vol-targeted** | **0.75x** | **$403.67** |

Vol targeting's value is now precise and it is **not alpha**: by truncating the left tail it
lets you carry *more* exposure under a fixed drawdown cap. 0.75x versus 0.50x, and **+44% more
terminal wealth under the identical risk rule.** That follows mechanically from the drawdown
reduction cycle 11 measured at 16 of 18 assets, p=0.0007.

Unconstrained, SPY still wins. Constrained — which is the world this project actually operates
in — vol targeting wins because it buys back exposure.

## H12.1 — an interior growth optimum exists (partly falsified)

Confirmed in shape, **falsified in my prediction.** I pre-registered "the optimum is below 2x."
It is 3.25x for SPY with margin charged at 5.75%. The curve is textbook:

| leverage | $100 becomes | maxDD |
|---|---|---|
| 1.00x | $422.52 | 33.8% |
| 2.00x | $742.56 | 58.8% |
| **3.25x** | **$954.45** | **79.2%** |
| 4.00x | $862.52 | 86.9% |
| 5.00x | $551.56 | 93.5% |

Recorded as a miss rather than quietly restated.

## H12.2 — margin interest moves the optimum (survives)

Free borrowing puts the optimum past 5x; charging Alpaca's sub-$25k rate of 5.75% moves it to
3.25x. Well past the 0.25x falsification bar. Cycle 6's leverage work assumed free money.

## H12.3 — no strategy beats SPY at matched-optimal leverage (refuted)

The apparent win: vol-targeted SPY at 4.25x turning $100 into $2,475 against SPY's $954.

It fails on two counts:

* **Stationary bootstrap**, 2000 paired resamples, 21-day blocks to preserve volatility
  clustering: 95% CI on the unlevered wealth gap is **[−$173.90, +$432.04]** — spans zero.
  Vol targeting **loses in 22.8% of resamples**.
* It rests entirely on a Sharpe difference of 0.17 that cycle 11 already refuted at z=1.01,
  p=0.31. **Leverage amplifies an edge that is not there.**

## H12.4 — the drawdown constraint binds long before the growth optimum (confirmed)

SPY's growth optimum is 3.25x, implying a 79.2% drawdown. The halt fires at 20%. The optimum is
unreachable by a factor of roughly 6.5x in leverage.

Separately: **constant leverage above ~3.5x goes to zero.** Not "underperforms" — bankrupt.
Enforcing Reg T maintenance calls *prevents* that, which is what margin calls are for; they are
a circuit breaker, not a cost.

## Errors found in this cycle's own analysis

Two, both in the same function, both of which produced a wrong and appealing answer:

1. **Debt held fixed instead of leverage.** Leverage decayed as the account grew, so 3x showed
   zero margin calls and high leverage looked survivable.
2. **Mismatched rebalancing.** The margin path rebalanced monthly against a daily-rebalanced
   baseline, so the table measured rebalance frequency and appeared to show that margin calls
   *create* wealth — a result that should be impossible and was the clue.

Both were caught by the number being implausible rather than by the code looking wrong. Cycle
11 contributed an unpaired t-test in the same spirit. **Three analysis errors across two
cycles, every one of which flattered the result.** That is the base rate any finding here
should be discounted by.

## What this changes

1. **The halt is the binding constraint, so it deserves engineering attention, not the signal.**
   Cycle 11 S4 already showed the resumption delay costs 8–20% of terminal wealth.
2. **Vol targeting is worth deploying** — as constraint relief worth +44% terminal wealth under
   the 20% rule, priced honestly at ~1.83 CAGR points/yr unconstrained.
3. **Leverage above 2x is off the table** regardless of what the growth curve says: Reg T caps
   initial leverage at 2x for equities anyway, and the drawdown rule caps it near 1x.
4. **Stop testing signals on this dataset.** Two cycles of the most careful work in this project
   have produced no alpha and three self-inflicted errors. The live paper account is the only
   uncontaminated data left.
