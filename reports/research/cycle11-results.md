# Cycle 11 — Results

Run 2026-08-18. Pre-registration: `cycle11-preregistration.md`, written before measurement.
Scripts: `cycle11_mechanisms.py`, `cycle11_costs.py`, `cycle11_attack.py`, `cycle11_batch2.py`,
`cycle11_overnight.py`.

## Headline

**The core finding is unchanged: nothing here beats SPY buy-and-hold on return.** Four
hypotheses survived falsification, and none of them is an alpha source. All four are about
risk, cost, or mechanics — which is what remains once an exhausted holdout rules out signal
discovery.

Three hypotheses were killed, one of them by catching an error in this cycle's own analysis.

## Survived

### S1 — Timing luck is worth 2.31 CAGR points and is removable
Twenty-one runs of one identical momentum rotation, differing **only** in which day of the
month it rebalances:

| | CAGR | Sharpe | maxDD |
|---|---|---|---|
| best offset (10) | 11.20% | 0.81 | 26.9% |
| worst offset (20) | 8.89% | 0.64 | 34.9% |
| 21-tranche blend | 10.63% | 0.78 | 27.4% |

No signal, parameter, or cost differs between them. **2.31 points of annual CAGR is decided by
an arbitrary calendar choice** — larger than most "edges" cycles 1–10 chased. Splitting the
portfolio into overlapping tranches converts that gamble into its expected value.

The deployed strategy rebalances on a single fixed date, so it currently holds this bet.

### S2 — Volatility targeting is drawdown control, not alpha
Exposure scaled by trailing realised vol, versus buy-and-hold, across 18 assets:

* Max drawdown **reduced on 16 of 18** — P(≥16 of 18 by chance) = **0.0007**
* Median cost: **−1.83 CAGR points per year**
* SPY: maxDD 33.8% → 20.1%

Its Sharpe improvement did **not** survive (see K1). What survives is the tail truncation,
which is mechanical: losses cluster in high-volatility regimes. This matters operationally
because the project halts at a 20% drawdown, and a halt stops trading entirely.

### S3 — Real execution cost is ~1bp, not the 5bps assumed
Measured NBBO, 2026-08-17 14:30–15:00Z:

| SPY | QQQ | IWM | EFA | VNQ | XLK | TLT | XLE | MDY | DBC |
|---|---|---|---|---|---|---|---|---|---|
| 0.26 | 0.41 | 0.66 | 0.92 | 1.02 | 1.05 | 1.22 | 1.60 | 1.96 | 3.31 |

Median ≈ 1.1bps against the 5bps used in earlier refutations — **5–20x too conservative**.

Method note worth keeping: the Corwin-Schultz high-low estimator gave 34bps for SPY, wrong by
two orders of magnitude, because a daily high-low range is mostly volatility and not spread.
It was replaced with real quotes rather than trusted.

This does **not** revive hypothesis #8 (short horizons), which was already tested down to an
unachievable 0.5bps and still lost. It does mean the deployed cost model is miscalibrated.

### S4 — The drawdown halt is cheap; the *resumption delay* is expensive
SPY, 20% halt, varying how long the system stays flat before re-entering:

| variant | CAGR | Sharpe | maxDD | $100 becomes |
|---|---|---|---|---|
| no halt | 15.36% | 0.90 | 33.8% | $453.73 |
| halt, resume after 5d | 15.95% | 0.96 | 29.8% | $479.28 |
| halt, resume after 21d | 14.33% | 0.91 | 25.4% | $412.70 |
| halt, resume after 63d | 12.97% | 0.87 | 28.3% | $363.69 |

The project's halt requires a human to clear it, which in practice is the 21d or 63d row —
**8% to 20% of terminal wealth**. The 5d row beating no-halt outright is a single path and
almost certainly inside noise; the robust part is the monotone direction: **longer delay is
strictly worse.** Automating a safe resumption path is worth more than any signal tested here.

## Killed

### K1 — Volatility targeting as an alpha source
Looked like the best result the project had produced: Sharpe 0.92 → 1.09, drawdown 41.5% →
20.1%, winning on all 12 parameter combinations, break-even cost 113x the real spread.

It died on the tests that matter:

* Jobson-Korkie/Memmel z = **1.01**, p = **0.31** — the two series are the same asset, and a
  naive independent-samples test would have badly overstated this
* Wins on **6 of 12 assets** — a coin flip
* Loses the most recent era (2025–2026) by −0.27 Sharpe

### K2 — The overnight/intraday effect
Batch 2 appeared to find it: overnight beat intraday on 6 of 8 assets with IWM t=3.47,
GLD t=3.82, QQQ t=3.10, all past the luck bar.

**That reading was wrong, and the attack caught it.** Those were one-sample t-tests on
overnight returns alone. The correct **paired** test drops SPY from t=2.74 to **0.63**, and
only **1 of 18** assets clears the bar.

The deeper error: "overnight beats intraday" is not "overnight-only beats buy-and-hold."
Buy-and-hold collects both sessions. Overnight-only beat buy-and-hold on CAGR on **1 of 18**
assets. The SPY premium also decayed from 7.93%/yr (2016–18) to −0.44% (2022–24).

### K3 — Turn-of-month
Positive on 4 of 7 assets, largest t = 1.57 (GLD) against a 2.82 bar. Nothing there.

## What this cycle changes

1. **Tranche the rebalance** — removes a 2.31-point annual gamble at no cost. Highest-value,
   lowest-risk change available.
2. **Automate halt resumption** — the delay costs 8–20% of terminal wealth.
3. **Recalibrate the cost model** — it assumes 5bps against a measured ~1bp.
4. **Treat vol targeting as a halt-avoidance tool**, priced honestly at ~1.83 CAGR points/yr,
   never as alpha.

## Method note

Cumulative trials now ~58; luck bar z > 2.85. Every result above is deflated against that, not
against this cycle's count. The one error found in this cycle's own work (K2) is recorded
rather than quietly corrected, because the error rate is itself evidence about how much any
result here should be trusted.
