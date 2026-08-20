# The engine can express `SPY_SMA200` — and the rule is not worth expressing

**Classification: `EXPLORATORY`.** Consumed window, no registration, no adversarial probes. This
diagnoses machinery and then checks the economic premise behind it. It spends no statistical budget
because it searches nothing: both questions were fixed in advance by `docs/per-session-trend-spec.md`.

Measured 2026-08-19 on SIP, 2016-01-04 to 2026-08-14, through `BacktestEngine` with the config's
own components applied.

## The question, open since cycle 15

`docs/per-session-trend-spec.md` states the success criterion:

> Success is the engine reproducing SPY_SMA200's 77% exposure and ~10.34% CAGR on the research
> history with `universe: [SPY]`. Anything short of that means the gate is still being applied
> somewhere monthly.

Six diagnoses were recorded against it. Three were refuted by measurement, two by reading, and the
sixth — mine, earlier today — was refuted by the measurement I ran to confirm it.

## Answered: it reproduces it

| `universe: [SPY]`, trend only | CAGR | Sharpe | exposure | trades | maxDD |
|---|---:|---:|---:|---:|---:|
| **`SPY_SMA200` target** | **10.63%** | **0.93** | **77.44%** | 25 | 19.50% |
| **engine, per-session gate, clean bars** | **10.56%** | **0.92** | **77.29%** | 26 | 21.66% |
| engine, monthly gate, clean bars | 8.86% | 0.82 | 72.53% | 24 | 23.45% |
| engine, per-session gate, corrupt bar present | 7.89% | 0.65 | 77.18% | 27 | 25.77% |
| engine, monthly gate, corrupt bar present | 6.68% | 0.58 | 72.50% | 25 | 24.46% |

**0.07 CAGR points, 0.01 Sharpe, 0.15 exposure points.** The criterion is met.

Four separate things had to be removed before it could be, which is why six diagnoses each found a
different culprit and each was right about a piece:

1. **`max_position_value_bps: 1000`** — a 10% cap per position. A single-name config can never
   deploy more than a tenth. Worth ~5x on exposure.
2. **The drawdown risk ladder** — halt at 15%. Invisible at a 10% cap because the position is too
   small to draw the account down that far; at full size it halts entry on 928 of 2669 sessions.
3. **`trend_gate_per_session` off** — monthly-only re-entry. Worth 4.76 exposure points and, with
   the other two held fixed, **1.70 CAGR points and 0.10 Sharpe**.
4. **One corrupt bar** — SPY 2026-02-02, low of 68.64 against a close of 691.70. Worth **2.67 CAGR
   points**, more than the trend gate itself.

Every earlier attempt varied one of these while another dominated, which is exactly why the
question survived six cycles. It was never one cause.

## The premise nobody checked

The spec calls `SPY_SMA200` "the one rule measured to beat the market". Measured against the market:

| | CAGR | Sharpe | exposure | maxDD | $100 becomes |
|---|---:|---:|---:|---:|---:|
| SPY buy-and-hold | **15.38%** | 0.90 | 100.00% | 33.79% | **$454.70** |
| `SPY_SMA200` | 10.63% | 0.93 | 77.44% | **19.50%** | $291.36 |

**It does not beat the market. It loses to it by 4.75 CAGR points and $163 per $100 invested.**
What it buys is a halved drawdown — 19.50% against 33.79% — for a Sharpe difference of +0.026.

That Sharpe difference is the entire case for the rule, so it is the thing to test. Paired, because
the two series hold the same asset 77% of the time and an unpaired test on correlated series is the
error `CLAUDE.md` names explicitly:

```
sessions 2667   correlation 0.6531
paired mean daily excess return of SMA200 over buy-and-hold:  -2.005 bps   t = -1.24
Sharpe of the difference series:  -0.380,  t = -1.24
years needed to reach t = 1.96 at this effect:  27
```

**The point estimate is negative and the result is not significant.** On the paired test — the
correct one — the rule underperforms holding SPY by 2 bps per session, and 10.6 years cannot
resolve it either way. It would take 27.

## What this establishes

**Engineering: the limitation recorded in `STATUS.md` is closed.** "This engine cannot express the
one rule measured to beat the market" is false. It expresses it to within 0.07 CAGR points once the
cap, the ladder, and the monthly gate are set correctly and the data is clean. `trend_gate_per_session`
is validated — its effect was never measured before because the other three effects were larger.

**Economics: the target was never worth chasing.** The rule loses to buy-and-hold on return and its
Sharpe edge is indistinguishable from zero with the data available. Six diagnostic cycles went into
reaching a benchmark that a paired test cannot separate from doing nothing.

**Risk: expressing it remains outside the deployed risk budget.** The reproducing configuration
draws down 21.66% — above the 15% halt tier and above the 20% liquidation tier. The rule's own
benchmark draws down 19.50%. A concentrated single-name config is not deployable under this ladder,
and **the ladder is the correct component of the two.** Nothing here is a proposal to widen it:
that would be loosening a drawdown control to make a backtest look better, and the backtest in
question loses to buy-and-hold anyway.

## The lesson worth keeping

The cheapest measurement in this document is the last one — comparing the target against holding
the asset — and it invalidates the goal that six cycles of work pursued. It cost one backtest and
could have been run first.

`CLAUDE.md` requires a power analysis before an expensive experiment. That discipline was applied
to hypotheses and not to *engineering targets*. A benchmark treated as a goal is a hypothesis about
what is worth building, and it deserves the same gate. The rule here would have failed it: an
effect of 0.026 Sharpe against 10.6 years of data is unresolvable before a single line is written.
