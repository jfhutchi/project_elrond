# Per-session trend evaluation — implementation spec

The highest-value engineering task left in this project, and it is now narrowed to one line.

## What is broken

`SPY_SMA200` — hold SPY while it is above its 200-day average — returns **10.34% CAGR at
Sharpe 0.91**, the highest measured anywhere here. Two configs tried to reproduce it inside the
strategy engine and reached 30% and 43% of the exposure they needed.

The cause is **not** position sizing. Adding `target_weight_sizing`, which removes the
ATR-derived risk cap entirely, changed nothing: 43.36% exposure either way.

The cause is **time in market**. Same trend condition, but:

| | invested on |
|---|---|
| SPY_SMA200 benchmark | **77% of sessions** |
| this engine | **43% of sessions** |

## The exact mechanism

`src/quantbot/strategy/adaptive_momentum.py:273`, inside `build_monthly_roster`:

```python
if (config.positive_momentum_required and momentum <= 0) or close <= trend:
    continue
```

The trend filter is applied **when the roster is built**, which happens once a month. If SPY
closes below its 200-day average on the final session of a month, it is excluded from the
roster for the *entire following month* — even if it recovers on the second session.

The benchmark re-checks the condition every session and re-enters the next day. Over 10.6 years
those missed re-entries are most of the return gap.

This is also why the effect looked like a sizing problem: a symbol absent from the roster cannot
be entered at any size, so every sizing knob appeared inert.

## The fix

The trend condition is a *hold-while-true* predicate, not a *selection* criterion, and the two
belong at different frequencies:

* **Selection** (which names are eligible, by momentum rank) is legitimately monthly. That is
  what the roster is for.
* **The trend gate** must be evaluated per session, where `evaluate_symbol` already runs.

So: stop excluding below-trend symbols at roster construction, and let the per-session
`asset_trend` component gate entry instead. It already exists and already runs every session.

Care required, in order of risk:

1. **Do not change behaviour for deployed configs.** v1.2.0 is trading. Gate this on a config
   field defaulting to today's behaviour, omitted from `canonical_configuration` at that
   default — the pattern `volatility_target_bps`, `rebalance_tranches` and
   `target_weight_sizing` all use, each pinned by an identity test.
2. **Roster size interacts.** With the trend filter removed from construction, more names
   qualify, so `roster_size` starts binding differently. Re-measure rather than assume.
3. **Turnover rises.** Re-entering mid-month costs spread. Measured spreads are ~1bp
   (`REFUTED.md`), so this is unlikely to bind, but it must be charged in the backtest.

## Status: implemented, NOT YET VALIDATED

`trend_gate_per_session` is implemented and gated off by default; deployed identities are
unchanged and pinned by test. **Its effect is unmeasured.**

The attempt to measure it was invalid and that is worth recording. `run_research_backtest.py`
runs the six fixed benchmark variants through `component_switches_for(variant)`
(`engine.py:212`), so `FULL_STRATEGY` enables every component regardless of what the config
file says. Three separate config changes — `target_weight_sizing`, `trend_gate_per_session`,
and the component switches themselves — all produced byte-identical output because **none of
them ever reached the code under test.**

That also retroactively weakens the earlier "43% vs 77% exposure" diagnosis: that number came
from the same runner and describes the fixed FULL_STRATEGY variant, not any config written
here. The exposure gap is real, but the attribution to roster-level trend filtering was not
established.

**The harness now exists**: `scripts/backtest_config.py`. It passes the config's own components
to `BacktestEngine`, so a run finally reflects the file. Building it exposed a second seam worth
knowing about: `StrategyComponents` (config) and `ComponentSwitches` (backtest) are *different
types* — the latter carries `atr_risk`, which the config cannot express — so they need explicit
conversion rather than being passed through.

**First valid measurement**, `strategy-trend-v4.yaml` with per-session trend gating,
target-weight sizing, and only `asset_trend` enabled:

| | exposure | CAGR | Sharpe | trades |
|---|---|---|---|---|
| SPY_SMA200 target | 77.37% | 10.34% | 0.91 | 25 |
| this config, measured properly | **27.28%** | 1.66% | 0.29 | **6** |

Six trades in 10.6 years against the benchmark's twenty-five. The config is not being held out
of the market by the roster trend filter — that is now disabled — it is barely *entering*. The
next question is which gate rejects entry, and `evaluate_symbol`'s reason codes are recorded per
signal, so the answer is available rather than needing to be guessed.

## The target

Success is the engine reproducing SPY_SMA200's 77% exposure and ~10.34% CAGR on the research
history with `universe: [SPY]`. Anything short of that means the gate is still being applied
somewhere monthly.

Note what this is and is not. It does not create alpha — cycles 15 and 16 established the
momentum ranking carries none. It lets the engine express a rule that was already measured to
work and which it currently cannot run.


## Next step, narrowed further

The entry gates are **not** the cause. `engine.py:686-696` applies, in order: ATR availability,
`market_regime`, `asset_trend`, `momentum`, `donchian_entry`. With trend-v4's components only
`asset_trend` is active, so entry requires SPY above its 200-day average — true on ~77% of
sessions, which is exactly the target exposure.

Exits are also correct: `roster_exit` is off, and `engine.py:644` exits on the same trend
condition.

So entries are permitted and exits are appropriate, yet the config trades 6 times and holds 27%
exposure. The loss is therefore **between candidate selection and a filled position** — most
likely roster interaction (`engine.py:601-605` builds the roster with `use_momentum=False`,
which may produce an empty or degenerate ranking) or sizing rejecting the order.

Two concrete places to look, in order:
1. Whether `build_monthly_roster` returns anything at all when `use_momentum` is False and the
   universe has one name. An empty roster with `roster_exit` disabled would permit entry but
   might still starve candidate construction.
2. Whether `size_entry` rejects: at `$100` with one name it must clear `MINIMUM_FRACTIONAL_
   NOTIONAL` and every cap. `RiskRejection.reasons` names the cap, so this is observable.

Do NOT guess between them. Instrument one run and read the reason codes — this document has
already been wrong twice by reasoning ahead of measurement.


## The invariant that should drive the next attempt

`trend_gate_per_session` is now also wired into the backtest engine's roster construction
(`engine.py:601`) — the earlier fix only touched `build_monthly_roster` in
`adaptive_momentum.py`, which the backtest never calls. That was a real bug and worth fixing.

**It did not help.** Exposure moved 27.28% -> 19.24% and CAGR 1.66% -> 1.65%.

The number that has not moved is **6 trades**, across every change tried:

| change | trades | exposure |
|---|---|---|
| baseline | 6 | 43.36%* |
| `target_weight_sizing` | 6 | 27.28% |
| `trend_gate_per_session` (wrong path) | 6 | 27.28% |
| `trend_gate_per_session` (engine path) | 6 | 19.24% |

\* from the invalid runner; the others are from `backtest_config.py`.

Six entries in 2,669 sessions is roughly one every two years. Sizing changes move *how much* is
held, and gate changes move *how long*, but neither moves *how often*. Something admits a
candidate only six times in a decade, and until that is identified every other knob is
rearranging the consequences of it.

**Instrument this specifically:** log every session where SPY is above its 200-day average but
no position is opened, and record which branch skipped it. Do not tune anything further first.
This document has now been wrong three times by reasoning ahead of measurement.


## The specific thing to check first

`engine.py:677` is `for symbol in active_roster:` — entry can only ever consider names in the
roster, and `active_roster` is rebuilt **only when `is_month_end`** (`engine.py:597`). So an
empty roster means zero entries for the whole following month, whatever the gates allow.

trend-v4 sets `momentum: false`, which reaches `rank_assets` as `use_momentum=False`.

**Hypothesis, untested:** `rank_assets` produces no rankings when `use_momentum=False`, leaving
`active_roster` empty most months and capping entries at the handful of months where something
slips through. That would explain a 6-trade count that no sizing or gating change has moved.

It is one assertion to check:

```python
rank_assets(sliced, config, require_trend=False, require_positive=False,
            use_momentum=False, as_of=timestamp)
```

If that returns an empty tuple for a single-name universe, this is the whole answer and the fix
is in `rank_assets`, not in any config field.

Stated as a hypothesis on purpose. This document has been wrong three times by reasoning ahead
of measurement, and the correct next action is to run that one call, not to act on this.
