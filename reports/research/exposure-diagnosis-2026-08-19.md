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

## The plateau, diagnosed the same day by measurement

The paragraph that stood here said a second constraint limited *position weight* to ~60% and that
I had not identified it. **That claim was wrong, and measuring it was what showed so.** At a 100%
cap the engine holds an average weight of **100.00%** — 1161 of 1161 held sessions at >=95%,
median 100.00%. There is no second constraint on weight. The ~60% figure came from dividing one
run's capital exposure by another run's time in market.

What actually collapses at a 100% cap is **time in market**, 72.50% -> 43.50%, taking trades from
25 to 16. Capital exposure and time in market become the same number at a 100% cap, which is why
43.50% appears in both columns.

The cause is the **drawdown risk ladder**, and it is measured rather than reasoned about:

| run (`universe: [SPY]`, trend only, 100% cap) | CAGR | Sharpe | exposure | time in mkt | trades | maxDD | halted | liquidating |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| deployed ladder 5/10/15/20, floor 0 | 4.94% | 0.59 | 43.50% | 43.50% | 16 | 16.29% | **928** | 0 |
| deployed ladder, halt floor 2500 | 4.43% | 0.53 | 44.29% | 44.29% | 18 | 20.51% | 0 | **868** |
| ladder 10/20/30/40, floor 0 | 6.68% | 0.58 | **72.50%** | 72.50% | **25** | 24.46% | 0 | 0 |
| ladder 20/40/60/80 (near-inert) | 6.68% | 0.58 | 72.50% | 72.50% | 25 | 24.46% | 0 | 0 |
| **`SPY_SMA200` target** | **10.34%** | **0.91** | **77.37%** | 77.37% | 25 | 19.50% | — | — |

At a 10% cap the position is too small to draw the account down 15%, so the ladder never fires and
its effect is invisible. At a 100% cap the same ladder halts entry on **928 of 2669 sessions**,
first firing 2020-06-11. Widening the ladder restores 72.50% exposure and all 25 trades — the
trend gate's full timing. **The ladder was the entire constraint on time in market.**

### The halt floor does not fix this, and that is informative

`drawdown_halt_floor_bps` is **0** in the deployed config, so the halt is absorbing exactly as
`risk/drawdown.py:41-46` describes. But setting the floor releases the halt only to hand the
account to the **liquidation** tier — 868 sessions — because the drawdown then runs to 20.51%
against a 20% liquidation trigger, and the floor deliberately does not touch liquidation. Net
effect: +0.79 exposure points and *worse* CAGR, 4.94% -> 4.43%. The floor is still the right fix
for the absorbing-halt defect; it is not a fix for this.

### What this actually establishes, and what must not be concluded from it

The deployed ladder — halt at 15%, liquidate at 20% — is calibrated for a ten-name rotation
holding 10% positions. A concentrated single-name SPY position has a natural drawdown *above both
triggers*: the `SPY_SMA200` benchmark itself draws down **19.50%**, and buy-and-hold SPY draws down
33.79%. The risk ladder is set below the natural drawdown of the rule being expressed.

**The conclusion is not "widen the ladder."** The widened runs above are diagnostic instruments,
not recommendations, and they are not a proposal to change any deployed value. They cost 24.46%
maximum drawdown to buy 6.68% CAGR — worse risk-adjusted performance (Sharpe 0.58) than the
deployed configuration's own benchmark. Loosening a drawdown control to make a backtest look
better is the specific move `CLAUDE.md` prohibits, and it is prohibited because it works.

The defensible conclusion is narrower and points the other way: **a concentrated single-name
config is not deployable under this risk ladder**, and the ladder is the correct component of the
two. `SPY_SMA200` is not reachable by this engine at full size without accepting a drawdown the
risk engine is deliberately built to refuse. That is a real and permanent limitation, but it is a
*risk-budget* limitation rather than the sizing bug the earlier note described.

### The per-session trend gate is re-promoted, and now quantified

With the ladder made inert the engine reaches 72.50% exposure and 25 trades, and still returns
**6.68% CAGR at Sharpe 0.58 against the benchmark's 10.34% at 0.91**, with a *deeper* drawdown
(24.46% vs 19.50%). Exposure differs by 4.87 points; return differs by 3.66 points and Sharpe by
0.33. The residual is entirely the monthly-rebalance lag — *when* the engine is invested, not how
much.

So the demotion recorded earlier in this document was itself premature. The trend gate is worth
more than its 4.87-point exposure gap suggests, because re-entering at the next month end rather
than the next session concentrates the missed sessions in exactly the recoveries that matter. This
is the first time that cost has been isolated from the sizing and risk-ladder effects, and it is
the first of the six diagnoses of this question to be measured with those two held fixed.

## What this changes

**The per-session trend gate survives, for a different reason than the one recorded.** Its
original premise — a 34-point timing gap — is false; the gap is 4.87 points. But with sizing and
the risk ladder both held fixed, those 4.87 points carry **3.66 CAGR points and 0.33 Sharpe**,
because the missed sessions cluster in recoveries. It is worth doing on the measured evidence, not
on the premise `STATUS.md` gave for it.

**A 10% position cap is correct for a 10-name rotation and wrong for a concentrated config.** It
is not an engine limitation. Any config meant to express a single-name rule needs its own cap,
and nothing prevented that being set.

**The finding is weaker than recorded, not stronger.** "This engine cannot express the one rule
measured to beat the market" is not established. What is established is that the deployed
*configuration* cannot, for a reason that is one line of YAML plus something still unknown.
