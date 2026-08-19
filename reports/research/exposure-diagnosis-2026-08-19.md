# The exposure gap is position size, not time in market

**Classification: `EXPLORATORY`.** Consumed window, no registration, no adversarial probes. This
diagnoses machinery; it tests no hypothesis and spends no statistical budget.

Measured 2026-08-19 through `BacktestEngine` with the config's own components applied
(`scripts/spy_only_exposure_diagnosis.py`), on SIP 2016-01-04 to 2026-08-14.

## The claim being checked

`STATUS.md` §6h, the project's central architectural finding:

> **Cause pinned: it is time in market, not position size.** Adding `target_weight_sizing` to
> remove the ATR risk cap entirely changed nothing — still 43% exposure, still 2.52% CAGR. This
> engine is invested on **43% of sessions** against the benchmark's **77%**, using the *same*
> 200-day condition.

## It is backwards

`exposure_fraction` is **capital** exposure — the mean of `gross_exposure / equity`
(`metrics.py:161`). It is not the fraction of sessions holding a position. The two are different
quantities, and the finding reads the first as the second.

Measured separately, `universe: [SPY]` with the trend gate and nothing else:

| | time in market | average weight when held | capital exposure |
|---|---:|---:|---:|
| `SPY_SMA200` benchmark | 77.37% | 100.00% | 77.37% |
| engine, SPY + trend only | **72.50%** | **11.67%** | 8.46% |

**The engine expresses the timing almost exactly.** 72.50% against 77.37% is a 4.87 point gap,
not the 34 points the finding describes. SPY is above its 200-day average on 83.64% of
post-warmup sessions, and the engine holds through 72.50% of all sessions.

The gap is entirely in *size*. `config/strategy-v1-2.yaml` sets `max_position_value_bps: 1000`
— a **10% cap per position** — so a single-name universe can never deploy more than a tenth of
equity. 11.67% average weight against 100%.

## Why the earlier test found nothing

`target_weight_sizing` removes the **ATR-derived** risk cap. It does not touch
`max_position_value_bps`, which is what binds here. The knob that was turned was not the
constraint, so of course nothing moved — the same shape as the disconnected-harness failures in
`REFUTED.md`, one layer down.

## Raising the cap, measured

| `max_position_value_bps` | CAGR | Sharpe | capital exposure | trades | max DD |
|---:|---:|---:|---:|---:|---:|
| 1000 (deployed, 10%) | 0.79% | 0.55 | 8.46% | 25 | 3.06% |
| 2500 | 1.92% | 0.55 | 20.53% | 25 | 7.24% |
| 5000 | 3.66% | 0.56 | 39.26% | 25 | 13.37% |
| 10000 (100%) | 4.94% | 0.59 | 43.50% | 16 | 16.29% |
| **`SPY_SMA200` target** | **10.34%** | **0.91** | **77.37%** | 25 | 19.50% |

A tenfold cap increase moves exposure roughly fivefold. **It then plateaus at 43.50%, well short
of 77.37%.**

## What remains undiagnosed, stated as undiagnosed

At a 100% cap the engine still averages only ~60% weight while held, and trade count falls from
25 to 16. Something other than `max_position_value_bps` limits deployment, and **I have not
identified it.** Candidates not yet distinguished: cash/commission reserve in `size_entry`,
volatility targeting, a second cap, or an interaction where a position that cannot be sized is
skipped entirely (which would explain 25 → 16 trades).

`docs/per-session-trend-spec.md` records five confident diagnoses of this question, three refuted
by measurement and two by reading. This document declines to be the sixth. The next step is to
read `RiskRejection.reasons` on the sessions where SPY is above trend and weight is below the
cap — the reason codes are already recorded per signal.

## What this changes

**The per-session trend gate is not the highest-value engine change.** `STATUS.md` names it so,
and the spec is built on the premise that the engine is out of the market too often. It is out of
the market ~5 points more than the benchmark, which is worth perhaps a fraction of the gap.
Position sizing is worth roughly five times more and is partly a **configuration value**.

**A 10% position cap is correct for a 10-name rotation and wrong for a concentrated config.** It
is not an engine limitation. Any config meant to express a single-name rule needs its own cap,
and nothing prevented that being set.

**The finding is weaker than recorded, not stronger.** "This engine cannot express the one rule
measured to beat the market" is not established. What is established is that the deployed
*configuration* cannot, for a reason that is one line of YAML plus something still unknown.
