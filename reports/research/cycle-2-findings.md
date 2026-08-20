# Research Cycle 2 — Findings

Hypotheses, candidate set, selection rule and promotion criteria were fixed in
[cycle-2-hypotheses.md](cycle-2-hypotheses.md) before any result below was observed.

## Setup

| | |
|---|---|
| Data | Alpaca SIP daily bars, split and dividend adjusted |
| Universe | 23 ETFs, unchanged from the shipped strategy |
| History | 2,669 sessions, 2016-01-04 → 2026-08-14 (10.6 years) |
| Warmup | 273 sessions |
| Selection | 2017-02-02 → 2024-06-28, five folds of ~1.5 years |
| Holdout | 2024-07-01 → 2026-08-14, 533 sessions, untouched during selection |
| Costs | 5bps per side, no commission |
| Initial cash | $100, fractional sizing |

Folds 3 and 4 contain the 2020 COVID crash and the 2022 bear market respectively, so the
selection region spans two distinct drawdown regimes rather than cycle 1's one.

## Selection region, median Sharpe across five folds

| Candidate | Median Sharpe | Median DD | Median return |
|---|---:|---:|---:|
| D7 dual + ATR | **1.00** | 6.10% | 9.46% |
| SPY buy & hold | 0.94 | 19.33% | 27.68% |
| D3 momentum + trend | 0.90 | 10.21% | 9.17% |
| SPY SMA200 | 0.89 | 13.97% | 21.12% |
| SPY Donchian | 0.86 | 8.61% | 12.50% |
| D8 full (shipped) | 0.78 | 3.36% | 3.25% |
| MOMENTUM_TREND | 0.75 | 12.92% | 11.44% |
| PURE_MOMENTUM_12_1 | 0.68 | 15.42% | 12.56% |
| D6 no gate, no stop | 0.67 | 7.48% | 7.41% |
| D5 no trailing stop | 0.31 | 4.23% | 2.59% |

D1 and D2 were excluded from ranking: their equity was flat for at least one whole fold,
leaving Sharpe undefined. They were not silently broken — run directly they produce 71 and
82 round trips and finish at $105.93 and $106.29 from $100 over 10.6 years. They are far
worse than D7, not better.

## Holdout, evaluated once

| | Return | Max DD | Sharpe | Trades |
|---|---:|---:|---:|---:|
| D7 dual + ATR | 15.66% | 5.38% | 1.22 | 23 |
| SPY buy & hold | 45.74% | 18.76% | 1.15 | 0 |

## Promotion decision: NOT PROMOTED

| Criterion | Result |
|---|---|
| 1. Beats shipped on selection median Sharpe | PASS — 1.00 vs 0.78 |
| 2. Holdout Sharpe ≥ 0.80 and ≥ SPY holdout | PASS — 1.22, vs SPY 1.15 |
| 3. Holdout drawdown within the 20% halt threshold | PASS — 5.38% |
| 4. Parameter neighbourhood not knife-edge | **FAIL** |

Neighbourhood detail, which had to stay above the shipped strategy's 0.78:

| Variation | Median Sharpe | |
|---|---:|---|
| `momentum_long` 252 → 189 | 0.65 | FAILS |
| `momentum_long` 252 → 315 | 0.65 | FAILS |
| `roster_size` 10 → 5 | 0.89 | ok |
| `roster_size` 10 → 15 | 0.41 | FAILS |

D7's advantage exists only at exactly `momentum_long=252`. Moving it one step in either
direction drops it below the strategy it was supposed to beat. That is the signature of a
result fitted to the sample rather than a property of the market, and it is why criterion 4
was written before results were seen.

The temptation is worth recording honestly: D7 passed three of four criteria, beat SPY on
risk-adjusted return out-of-sample, and did it with a quarter of the drawdown. Deploying it
anyway would have been the single most defensible-looking mistake available.

The holdout margin is also thin on its own terms — Sharpe 1.22 vs 1.15 across 23 trades is
nowhere near enough to separate skill from luck, and it earned a third of the return.

## Off-protocol observation, deliberately not acted on

While diagnosing D1 and D2, a variant outside the registered set — momentum + roster exit +
ATR sizing, without the regime filter — finished at **$195.55 from $100**, better than D7's
$168.46 and the best figure seen in either cycle.

It is not actionable. It was not pre-registered, and the holdout had already been consumed
by the time it appeared, so there is no clean data left to score it against. Recorded here
as a cycle 3 hypothesis and nothing more.

For scale, before it looks impressive: $100 → $195.55 took **9.53 years** of trading and
never crossed $200. That is roughly 7.3% annually, against SPY's substantially higher return
over the same span.

## The binding constraint going forward

**The 2024-07 → 2026-08 holdout is now burned.** It has been used. Any future candidate
scored against it is no longer out-of-sample, and there is no further history to carve —
Alpaca's SIP daily bars begin 2016-01-04.

This makes the live paper account the only uncontaminated out-of-sample data this strategy
family will ever receive. Every session it runs produces observations no backtest has seen.

## Classification

**NEGATIVE EVIDENCE**, consistent with cycle 1. Across two cycles, 10.6 years, two bear
markets and 31 candidates, no configuration of this strategy family has demonstrated an edge
that survives its own robustness check. Nothing has been deployed.

This is the expected outcome given published evidence: roughly half of an anomaly's alpha
disappears after publication, momentum's premium has fallen from about 10% annually in the
1990s to about 2%, and trend systems structurally lag in bull markets — which most of this
sample was.

## Reproducibility

The study was executed twice from a cold start and produced numerically identical results,
confirming the engine is deterministic. Inputs are pinned by an `ExperimentManifest` hashing
the strategy configuration, the full bar set, and the git commit.
