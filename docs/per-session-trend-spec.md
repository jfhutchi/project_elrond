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

## The target

Success is the engine reproducing SPY_SMA200's 77% exposure and ~10.34% CAGR on the research
history with `universe: [SPY]`. Anything short of that means the gate is still being applied
somewhere monthly.

Note what this is and is not. It does not create alpha — cycles 15 and 16 established the
momentum ranking carries none. It lets the engine express a rule that was already measured to
work and which it currently cannot run.
