# Research Cycle 8 — Findings: Meta-Labelling

Pre-registered in [cycle-8-hypotheses.md](cycle-8-hypotheses.md) before any result below was
observed.

## Verdict: NOT PROMOTED — does not clear the luck threshold

The meta-model appears to improve the primary strategy substantially and does not survive
the statistics.

## Setup

| | |
|---|---|
| Primary strategy | deployed configuration, all components enabled |
| Labelled trades | 411 completed trades over 10.6 years |
| Usable samples | 411 |
| Model | ridge-penalised logistic regression, pure Python |
| Features | ATR%, 12-1 momentum, distance to trend, regime state, SPY 21d vol, breakout extent |
| Validation | expanding-window walk-forward, trains only on trades CLOSED before the test window minus a 5-day embargo |
| Folds | 4 |

Two deviations from pre-registration, both recorded before running: gradient-boosted trees
were dropped for logistic regression alone, because 411 examples cannot support tree
ensembles and a smaller model keeps the trial count countable; and no clean holdout exists,
so the ceiling was always CANDIDATE PENDING FORWARD CONFIRMATION.

## Results

Unfiltered baseline: expectancy +0.135%, profit factor 1.12, win rate 40.9%.

| Fold | Train | Test | Taken | Declined | Expectancy filtered | Expectancy all | Delta |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 77 | 82 | 12 | 70 | +0.652% | +0.088% | +0.564% |
| 2 | 162 | 82 | 7 | 75 | **−0.713%** | −0.056% | −0.657% |
| 3 | 242 | 82 | 7 | 75 | +0.814% | +0.373% | +0.441% |
| 4 | 322 | 82 | 13 | 69 | +1.584% | −0.131% | +1.715% |

Pooled out-of-fold:

| | Expectancy | Profit factor | trade-Sharpe | n |
|---|---:|---:|---:|---:|
| Filtered | +0.747% | 2.29 | 1.72 | 39 |
| All trades | +0.069% | 1.06 | 0.38 | 328 |

Taken at face value that is a large improvement: profit factor more than doubles.

## Why it fails

The `trade-Sharpe` as implemented is expectancy / stdev × √n, which is the t-statistic.
Testing it properly:

* n = 39, t = 1.72, **two-tailed p = 0.085** — not significant at the 5% level.
* Per-trade standard deviation 2.71%, standard error 0.434%.
* **95% confidence interval on filtered expectancy: [−0.104%, +1.598%]** — contains zero.
* Cumulative candidate evaluations against this dataset across cycles 1-8: **~43**. The
  expected highest t-statistic among 43 zero-skill trials is **2.22**. Observed 1.72 does not
  clear it.
* Fold 2 lost money (−0.713%), so the pooled result rests on folds 1, 3 and 4.
* The model declines 88% of trades, leaving **5.2 trades per year**. Even were the edge real,
  that frequency would take decades to demonstrate.

## A defect found in this cycle's own code

The first run used a feature vector that duplicated `dist_to_trend` and omitted `momentum`
entirely, contradicting the declared feature list. Fixed and re-run before any conclusion was
drawn. The corrected result was directionally similar (t 1.72 vs 1.88), so the bug did not
create the apparent improvement — but it would have invalidated the stated method, and it is
recorded because a study whose features do not match its pre-registration is not the study it
claims to be.

## Hypotheses assessed

* **H1** — that a meta-model can identify a materially better subset: **not supported.** The
  apparent improvement does not survive significance testing or trial deflation.
* **H2** — that importance would concentrate on volatility and regime rather than momentum
  ranking: **untested.** With the result inside the noise band, interpreting coefficients
  would be reading structure into noise.
* **H3** — the null, that no meta-model beats the unfiltered strategy after deflation:
  **supported.**

## What this closes

Cycle 8 was the machine-learning avenue, framed in the way most likely to work: a small
hypothesis space, a binary label, the strategy itself as prior, and validation that cannot
leak. It still produced a result indistinguishable from luck on 411 labelled examples.

That is consistent with everything prior. AIEQ ran IBM Watson over institutional data for
nine years of live capital and underperformed the S&P every year. The constraint is not model
sophistication; it is that 411 trades cannot support a claim of skill, and that the edge the
strategy would need is not present in the data.

**The ML avenue is now closed with a recorded reason**, rather than remaining a permanently
open "maybe machine learning would work". Reopening it honestly requires more labelled
trades, and the only uncontaminated source of those is the live paper account going forward.
