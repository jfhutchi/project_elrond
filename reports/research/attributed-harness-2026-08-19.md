# The deployed config, measured through the engine that runs it

**Classification: `EXPLORATORY`.** The window is consumed, this run carries no registration,
cleared no power gate and ran no adversarial probes. It is a diagnostic of the harness and a
re-measurement of an existing diagnosis, not evidence of an edge.

Generated 2026-08-19 by `scripts/run_research_backtest.py` after the harness fix in `4644c3a`.

## Why this run exists

`REFUTED.md` records that three of this project's analysis errors came from a runner not
connected to the code under test. The cause was one missing argument: `engine.run(histories,
variant)` falls back to the *fixed comparator* switches, so every row of the old report ignored
the config's own component selection, and three config changes produced identical output.
`STATUS.md` §6h consequently marked its central diagnosis unattributed.

This is the first run measuring the deployed configuration with **its own components applied**.

## Result — SIP, 2,669 sessions, 2016-01-04 to 2026-08-14

Config `strategy-v1-2.yaml` (`adaptive-momentum 1.2.0`, hash `309894d8d8a5296e`), $100 initial,
5bps/side, 0 bars dropped.

| variant | total | CAGR | max DD | Sharpe | exposure | trades |
|---|---:|---:|---:|---:|---:|---:|
| SPY_BUY_AND_HOLD | 354.70% | **15.37%** | 33.79% | 0.90 | 100.00% | 0 |
| SPY_SMA200 | 183.50% | 10.34% | 19.50% | **0.91** | **77.37%** | 25 |
| PURE_MOMENTUM_12_1 | 182.24% | 10.29% | 28.12% | 0.77 | 89.81% | 643 |
| MOMENTUM_TREND | 132.78% | 8.30% | 19.90% | 0.76 | 83.35% | 654 |
| **CONFIGURED_STRATEGY** | **52.46%** | **4.06%** | 9.09% | **0.69** | **41.95%** | 272 |
| SPY_DONCHIAN | 52.35% | 4.05% | 24.55% | 0.49 | 55.75% | 26 |
| FULL_STRATEGY | 5.41% | 0.50% | 7.84% | 0.15 | 24.06% | 411 |

Components applied: `momentum`, `asset_trend`, `atr_risk`, `roster_exit` on; `market_regime`,
`donchian_entry`, `donchian_exit`, `trailing_stop` off.

## The harness reproduces every published benchmark figure exactly

This is the check that makes the new row trustworthy, and it is the reason to believe the fix
rather than the reason to believe the number.

| figure | recorded in | this run |
|---|---|---|
| SPY buy-and-hold 15.36% CAGR, Sharpe 0.90, 33.79% DD | `REFUTED.md` | 15.37%, 0.90, 33.79% |
| SPY_SMA200 10.34% CAGR, Sharpe 0.91, 19.50% DD, 77% exposure | `STATUS.md` §6h | 10.34%, 0.91, 19.50%, 77.37% |
| Full shipped strategy 0.50% CAGR, Sharpe 0.15 | `REFUTED.md` #2 | 0.50%, 0.15 |

Every comparator lands on its published value. The one row that could not previously be produced
at all is `CONFIGURED_STRATEGY`.

## What this establishes

**1. The harness is connected.** `CONFIGURED_STRATEGY` (4.06% CAGR, 41.95% exposure) differs
sharply from `FULL_STRATEGY` (0.50%, 24.06%), which is the row the old runner reported in its
place. Any conclusion the old runner supported about "the strategy" was a conclusion about a
fixed comparator — the deployed config performs roughly **eight times better** than the number
that stood in for it.

**2. `STATUS.md` §6h's exposure diagnosis survives re-measurement, and is now attributed.** The
claim was that the engine is invested on ~43% of sessions against the benchmark's 77%, using the
same 200-day condition, and that the gap is *time in market* rather than position sizing.
Measured attributably: **41.95% against 77.37%.** The diagnosis holds and its "unattributed"
caveat can be lifted.

**3. The deployed config still loses to buy-and-hold by a wide margin.** 4.06% CAGR against
15.37%, Sharpe 0.69 against 0.90. `REFUTED.md` #2 stands; it is now attributable rather than
new.

## What this does *not* establish

**No hypothesis is confirmed or refuted.** Cycles 2-10 searched this window and
`window_consumption()` returns `EXHAUSTED`. Under the architecture in #5/#19/#8 this run is
exploratory by construction, and nothing here may be cited as confirmatory evidence — including
the exposure figure, which corroborates a prior diagnosis rather than testing a frozen claim.

**Sharpe 0.69 is not an edge.** The minimum detectable Sharpe over this window at the current
trial count is ~0.89. A measured 0.69 is inside the noise band and would be `UNDERPOWERED` if
registered, which is the honest reading rather than a disappointing one.

**A companion IEX run** (1,520 sessions, 2020-07-27 onward) gave 3.64% CAGR, Sharpe 0.61, 33.82%
exposure for the same config. It is a different feed over roughly half the history and is
recorded only to show the two agree in direction.

## What follows

The 43%-vs-77% finding is confirmed, so the per-session trend gate work in
`docs/per-session-trend-spec.md` rests on a measured foundation rather than an unattributed one.
Moving that gate out of roster construction remains the highest-value engine change available.
