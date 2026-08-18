# Research Cycle 8 — Meta-Labelling (Pre-Registered)

Written before any cycle-8 result was observed.

## The idea, and why it is different from cycles 1-7

Cycles 1-7 all searched for a better *primary* signal — which symbols to hold. Every one
failed: the deployed configuration returns 0.50% CAGR, the best variant found lost to SPY
buy-and-hold out-of-sample, and no candidate survived its own robustness check.

Meta-labelling (López de Prado) does not search for a better signal. It takes the existing
signal as given and trains a secondary model on one narrow question: **when this strategy
fires, will the trade work?** The primary model decides direction; the meta-model decides
whether to act, and with what size.

Why this is a smaller and more honest hypothesis space than cycles 1-7:

* The primary signal is fixed, so the search is not over "which of 2^8 component
  combinations and 5 parameter grids wins".
* The label is binary (did this trade end profitable), not a continuous return forecast.
* The prior is the strategy itself. The meta-model can only decline trades, never invent
  them, so it cannot manufacture exposure the strategy never asked for.

## Stated up front: the sample is thin, and that governs everything

The deployed strategy produced **411 completed trades** across 10.6 years. That is the
entire labelled dataset. It is small for any machine-learning method, and it is the single
biggest reason to expect this to fail.

Concretely, from cycle 2's deflated-Sharpe work: **31 hand-chosen trials** already raised the
expected-best-under-no-skill threshold to 0.439 Sharpe. A hyperparameter search is thousands
of trials, and that threshold grows with the number attempted. Any result here must be
deflated by the actual trial count, not reported raw.

## Hypotheses

**H1 (primary).** A meta-model trained on trade features available *at signal time* can
identify a subset of the primary strategy's trades with materially better expectancy than
the full set, raising portfolio Sharpe by declining the rest.

**H2.** Feature importance will concentrate on volatility and regime state rather than on
the momentum ranking, because cycles 1-7 showed the ranking carries little information and
cycle 4 showed volatility sizing carries most of what does work.

**H3 (the null, and the expected outcome).** No meta-model beats the unfiltered strategy
once the result is deflated for the number of trials. Prior evidence: AIEQ ran IBM Watson
over institutional data for nine years of live capital and underperformed the S&P every
year; roughly half of published anomaly alpha disappears post-publication; and 411 labelled
examples cannot support a model with meaningful capacity.

## Features, fixed in advance

Only information available strictly before the entry fill. No feature may reference the
trade's own outcome, the bar it enters on, or any later bar.

1. `atr_pct` — ATR / price at signal
2. `momentum` — the primary 12-1 momentum score
3. `dist_to_trend` — (close − SMA200) / close
4. `regime_on` — SPY above its own SMA200
5. `spy_vol_21d` — realised 21-day SPY volatility
6. `roster_rank` — the symbol's rank within the monthly roster
7. `breakout_extent` — (close − entry channel) / ATR
8. `sleeve` — equity / bond / real-asset class of the symbol

## Method, fixed in advance

* Label: 1 if the completed trade's net P&L > 0, else 0.
* Model: gradient-boosted trees and logistic regression only. No deep learning — 411
  examples cannot support it, and a small model keeps the trial count countable.
* Validation: **purged, embargoed walk-forward.** Training folds must exclude any trade
  overlapping the test window, because trades span weeks and naive k-fold leaks the answer.
* Holdout: none is clean. Cycles 2-7 consumed every out-of-sample window and there is no
  further history — SIP begins 2016-01-04. This cycle therefore **cannot produce a
  deployable verdict**, only a hypothesis for the live forward data to test.
* Every result reported with its deflated Sharpe and the exact trial count.

## Promotion criteria, fixed in advance

A meta-model is promoted only if **all** hold:

1. It raises the primary strategy's Sharpe on purged walk-forward folds.
2. The improvement survives deflation for the full trial count.
3. It declines trades for reasons that are economically legible, not opaque.
4. It beats SPY buy-and-hold on risk-adjusted return — the bar every prior cycle failed.
5. It is confirmed on genuinely forward data the model has never seen.

Criterion 5 cannot be met today. The live paper account is the only source of such data, so
the honest ceiling for this cycle is **CANDIDATE PENDING FORWARD CONFIRMATION** — never
`PROMOTED`. If the walk-forward result is negative, cycle 8 closes the ML avenue with a
recorded reason rather than leaving it as a permanently open "maybe ML would work".
