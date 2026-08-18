# Research Cycle 2 — Pre-Registered Hypotheses

Written before any cycle-2 result was observed. Recorded so the candidate set cannot be
quietly expanded after the fact, which is how a search turns into a curve fit.

## What cycle 1 established

Over 2020-07 → 2026-08 (IEX, 5 years usable), the shipped strategy returned 0.30% CAGR
against SPY's 17.31%, with Sharpe 0.11 against 1.05. On a holdout it had never seen, the
best candidate selected by walk-forward returned 9.38% against SPY's 33.59%, and lost on
Sharpe too (0.98 vs 2.02). Classification: NEGATIVE EVIDENCE.

Diagnostics that survived that study:

1. Exposure was 19%. The strategy sits in cash most of the time, and the cash drag rather
   than bad selection is what destroys the return.
2. Removing the trailing stop was the single largest improvement (median fold Sharpe
   0.11 → 0.50), larger than any parameter change.
3. `donchian_exit` and `atr_risk` are inert: ablating either changes nothing, and varying
   `exit_period` across 13/20/34 changes nothing. The 3-ATR trailing stop fires before a
   20-day channel exit can, and the 10% position cap binds before ATR risk sizing does.
4. Among strategy variants, plain 12-1 relative momentum scored best (median fold Sharpe
   0.72) and came closest to buy-and-hold.

## Why cycle 1 was not a fair test of the underlying idea

Cycle 1 varied components **one at a time** from the full strategy. That measures marginal
contribution, not whether a coherent alternative design works. Interactions dominate here:
the entry gate and the trailing stop both push toward cash, so removing either alone still
leaves the other suppressing exposure.

Cycle 2 therefore tests whole designs, not marginal removals. It also runs on 10.6 years of
SIP data (2016-01 → 2026-08, 2,669 sessions) rather than 5 years of IEX, which adds the
2020 COVID crash as a second bear-market regime alongside 2022.

## Hypotheses

**H1 (primary).** The strategy's deficit is cash drag caused by entry timing, not by the
momentum ranking. A design that keeps the ranking and the regime gate but discards the
breakout entry and the trailing stop will achieve materially higher risk-adjusted return
than the shipped strategy.

**H2.** Absolute momentum (a market regime switch) contributes more than per-asset trend
filtering, because it is the mechanism with the strongest published support and it acts on
the whole portfolio rather than trimming individual names.

**H3 (null worth stating).** No variant in this family beats SPY buy-and-hold on
risk-adjusted return over this sample. Published evidence predicts this: roughly half of an
anomaly's alpha disappears after publication, momentum's premium has fallen from about 10%
annually in the 1990s to about 2%, and trend systems structurally lag in bull markets. Ten
of the last eleven years were a bull market.

## Pre-registered candidate designs

Eight designs. No more will be added after results are seen.

| Design | Components enabled | Rationale |
|---|---|---|
| `D1-dual-momentum` | momentum, market_regime, roster_exit | Antonacci's design; the best-published form of the idea |
| `D2-relative-only` | momentum, roster_exit | Isolates the ranking signal with no gating whatsoever |
| `D3-momentum-trend` | momentum, asset_trend, roster_exit | Per-asset trend filter instead of a market-wide switch |
| `D4-both-filters` | momentum, asset_trend, market_regime, roster_exit | Both filters, no entry timing or stops |
| `D5-no-trailing-stop` | full minus trailing_stop | Cycle 1's best single ablation |
| `D6-no-gate-no-stop` | full minus donchian_entry, trailing_stop | Removes both identified sources of cash drag |
| `D7-dual-plus-atr` | momentum, market_regime, roster_exit, atr_risk | Does volatility-scaled sizing add value once gates are gone? |
| `D8-full` | all | The shipped strategy, as reference |

## Selection rule, fixed in advance

Highest median Sharpe across the five walk-forward folds; ties broken by lower median
maximum drawdown. Benchmarks are reported alongside but are not selectable.

Exactly one design is then evaluated on the holdout, once.

## Promotion criteria, fixed in advance

A design is promoted to a new paper strategy version only if **all** hold:

1. It beats the shipped strategy on median fold Sharpe in the selection region.
2. On the holdout it achieves Sharpe ≥ 0.80 **and** ≥ SPY buy-and-hold's holdout Sharpe.
3. Its maximum drawdown on the holdout stays within the configured 20% halt threshold.
4. Its parameter neighbourhood is not knife-edge: the design still beats the shipped
   strategy when `momentum_long` and `roster_size` are moved one step in either direction.

Failing any of these, the result is recorded and **nothing is deployed**. A design that
wins the selection region but loses the holdout is a curve fit, and that is a finding, not
a setback.
