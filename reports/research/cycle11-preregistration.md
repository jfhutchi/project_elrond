# Cycle 11 — Pre-registration

Written **before** any measurement. Cumulative prior trials: ~45.

## The constraint that shapes this cycle

The holdout is exhausted. SIP history begins 2016-01-04 and cycles 2–10 consumed every
out-of-sample window. **Therefore no signal-discovery hypothesis tested on this dataset can
produce a credible result.** Anything found by searching this data for a better predictor is,
by construction, in-sample and already paid for 45 times over.

That eliminates the entire category of "find a better signal" — which is what cycles 1–10 were.
Two categories remain that are not blocked by an exhausted holdout:

**Class A — mechanism-driven.** The effect follows from construction or arithmetic, not from
fitting. The data is used to measure *magnitude*, not to *discover* the effect. A hypothesis
here can be believed without holdout because it was never a search.

**Class B — new data axis.** Data this project has never mined, so a first look at it is not
a 46th pass over the same numbers. Costs holdout on that axis, and must be corrected.

## Hypotheses

### H11.1 — Timing luck is material and removable (Class A)
**Claim.** Monthly roster rotation on one fixed date injects variance unrelated to skill. Two
identical strategies rebalancing a week apart hold different portfolios for reasons that carry
no information. Averaging N overlapping tranches reduces this dispersion.
**Why it can survive.** Variance reduction by averaging is arithmetic, not a fitted effect.
**Falsified if.** Dispersion of CAGR across rebalance offsets is < 1 percentage point, i.e. the
effect exists but is too small to bother with.

### H11.2 — Volatility targeting makes leverage survivable (Class A)
**Claim.** Cycle 6 found leverage breaches the 20% drawdown halt. Scaling exposure inversely to
realized volatility reduces drawdown for the same average exposure, because losses cluster in
high-volatility regimes.
**Why it can survive.** Vol clustering is among the most robust facts in finance and is not
being discovered here; only its magnitude on this series is measured.
**Falsified if.** Vol-targeted SPY does not improve Sharpe **and** does not reduce max drawdown
versus fixed exposure at matched average leverage.

### H11.3 — The 5bps execution assumption is wrong for this universe (Class A)
**Claim.** H8 (short horizons) and H9 (options) both died on execution cost at an *assumed*
5bps. The traded universe is large liquid ETFs whose real spread is far tighter. If actual cost
is ~1bp, the horizon conclusion may be wrong for the wrong reason.
**Why it can survive.** This is a measurement of a market fact, not a backtest.
**Falsified if.** Measured median spread across the universe is ≥ 5bps.

### H11.4 — Post-earnings-announcement drift (Class B)
**Claim.** The most replicated anomaly in the literature; never tested here. Prices drift in the
direction of an earnings surprise for weeks.
**Falsified if.** Drift is not distinguishable from zero after the cumulative-trials correction,
or is smaller than measured execution cost.

### H11.5 — Analyst estimate revision momentum (Class B)
**Claim.** Revisions to forward estimates predict returns; a distinct axis from price momentum.
**Falsified if.** As H11.4, or if the data cannot be obtained point-in-time (look-ahead makes
any result meaningless, and a look-ahead-contaminated result must be reported as unusable, not
as a success).

## Correction

Every result is deflated against **cumulative** trials (~45 + this cycle's), not this cycle's
count. A hypothesis is reported "possible" only if it clears that bar, and Class B results must
additionally be honest that the axis is now burned for future testing.

"Possible" here means: survived a falsification attempt. It does not mean profitable.

---

# Batch 2 — pre-registered after batch 1 results, before batch 2 measurement

Batch 1 outcome in one line: H11.2 died as an alpha claim (z=1.01, p=0.31, wins on 6 of 12
assets) but its drawdown reduction held on 10 of 12. That splits into a narrower hypothesis,
and three genuinely untested ones. Cumulative trials after this batch: ~54, luck bar z > 2.82.

### H11.2b — Vol targeting is drawdown control, not alpha (Class A)
**Claim.** The Sharpe improvement was noise. The drawdown reduction is not: losses cluster in
high-volatility regimes, so cutting exposure when vol rises mechanically truncates the left
tail. Value here is operational, not directional — this project halts at 20% drawdown, and a
halt ends trading entirely.
**Falsified if.** Drawdown reduction does not hold on a clear majority of assets, or costs more
CAGR than the halt it avoids would have.

### H11.6 — The 20% drawdown halt hurts more than it helps (Class A/B)
**Claim.** Halting after a 20% drawdown sells near a bottom and forgoes the recovery. The rule
is inherited and has never been tested against the alternative of riding through.
**Why it matters.** If true, the project's own safety rule is its largest single cost.
**Falsified if.** Halting produces equal or better terminal wealth than not halting.

### H11.7 — Overnight returns dominate intraday (Class B, new axis)
**Claim.** A large replicated literature finds close-to-open returns account for nearly all of
equity index return, with open-to-close near zero. Never tested here, and testable directly
from OHLC already held.
**Falsified if.** The overnight/intraday split is not large, or the implied strategy's two
trades per day at measured spreads consume the difference.

### H11.8 — Turn-of-month effect (Class B, new axis)
**Claim.** Returns concentrate around month boundaries. Documented; never tested here.
**Falsified if.** The effect does not clear the cumulative-trials bar.
