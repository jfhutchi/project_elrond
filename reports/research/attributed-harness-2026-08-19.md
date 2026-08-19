# The deployed config, measured through the engine that runs it

**Classification: `EXPLORATORY`. This is not confirmatory evidence and cannot become any.**

Generated 2026-08-19 by `scripts/run_research_backtest.py` after the harness fix in `4644c3a`.

## Why this run exists

`REFUTED.md` records that three of this project's analysis errors came from a runner that was
not connected to the code under test. The cause was one missing argument: `engine.run(histories,
variant)` falls back to the *fixed comparator* switches, so every row in the old report ignored
the config's own component selection and three config changes produced identical output.

This is the first run in which the deployed configuration is measured with **its own components
applied**.

## Result

Window: 1,520 sessions, 2020-07-27 to 2026-08-14. Alpaca **IEX** daily bars, dividend and split
adjusted. Config `strategy-v1-2.yaml` (`adaptive-momentum 1.2.0`, hash `309894d8d8a5296e`),
$100 initial, 5bps/side.

| variant | total | CAGR | max DD | Sharpe | exposure | trades |
|---|---:|---:|---:|---:|---:|---:|
| SPY_BUY_AND_HOLD | 161.98% | **17.31%** | 24.51% | **1.05** | 100.00% | 0 |
| SPY_SMA200 | 76.54% | 9.88% | 19.49% | 0.94 | 68.82% | 15 |
| PURE_MOMENTUM_12_1 | 83.40% | 10.58% | 16.66% | 0.84 | 83.22% | 319 |
| MOMENTUM_TREND | 49.36% | 6.88% | 17.23% | 0.66 | 75.15% | 326 |
| **CONFIGURED_STRATEGY** | **24.03%** | **3.64%** | 10.87% | 0.61 | 33.82% | 140 |
| SPY_DONCHIAN | 21.00% | 3.21% | 24.57% | 0.40 | 55.13% | 16 |
| FULL_STRATEGY | 1.84% | 0.30% | 5.33% | 0.11 | 18.66% | 193 |

Components actually applied: `momentum`, `asset_trend`, `atr_risk`, `roster_exit` on;
`market_regime`, `donchian_entry`, `donchian_exit`, `trailing_stop` off.

## What this establishes

**The harness is connected.** `CONFIGURED_STRATEGY` differs from every fixed variant, including
`FULL_STRATEGY`, which is the row the old runner reported in its place. The difference is large:
**0.30% CAGR against 3.64%**, and 18.66% exposure against 33.82%. Any conclusion drawn from the
old runner about "the strategy" was a conclusion about `FULL_STRATEGY`, not about what is
deployed.

**The deployed config still loses to buy-and-hold, by a wide margin.** 3.64% CAGR against
17.31%, Sharpe 0.61 against 1.05. That is consistent with `REFUTED.md` #2 and adds no new
information — it is the same finding, now attributable.

## What this does *not* establish

**These numbers are not comparable to the figures in `STATUS.md` and `REFUTED.md`.** Those were
measured on 2,669 sessions of **SIP** data from 2016-01-04; this is 1,520 sessions of **IEX**
data from 2020-07-27. Different feed, different window, roughly half the history, and no 2022
bear market at full weight. The exposure figure here (33.82%) is **not** a correction of the
"43% versus 77%" diagnosis in STATUS.md §6h — that diagnosis was on different data and remains
unattributed and un-remeasured.

**No hypothesis is confirmed or refuted by this.** The window is consumed: cycles 2-10 searched
it, and `window_consumption()` returns `EXHAUSTED`. Under the architecture in `#5`/`#19`/`#8`
this run is `EXPLORATORY` by construction — it carries no registration, cleared no power gate,
and ran no adversarial probes. It is a diagnostic of the harness, not a measurement of an edge,
and it is filed here rather than in the refutation table for that reason.

## What follows

Re-measuring the cycle-16 architectural finding requires the SIP research database and the same
attributed runner. That is now possible and has not been done.
