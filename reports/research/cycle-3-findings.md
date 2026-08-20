# Research Cycle 3 — Ensembles

## Why this cycle exists

Cycles 1 and 2 established that no single configuration of the momentum family is
distinguishable from noise, and that the data cannot resolve one: D7's holdout Sharpe of
1.223 carried a 95% interval of [-0.128, 2.574]. Separating its 0.074 margin over SPY would
require roughly 700 years of daily observations.

The literature points to a structural escape rather than a better signal. Sharpe scales
with the square root of the number of *uncorrelated* strategies, so four honest 0.5-Sharpe
components reach 1.0 together — and unlike a tuned parameter, that gain is not obtained by
fitting. Higher portfolio Sharpe is also detectable in less data, which attacks the
sample-size problem directly.

## The central finding: signal variety is not diversification

Four strategies from different signal families were simulated over 2017-02 → 2026-08.

| Pair | Correlation |
|---|---:|
| momentum-12-1 vs reversal-5d | 0.75 |
| momentum-12-1 vs absolute-trend | 0.68 |
| reversal-5d vs absolute-trend | 0.62 |
| momentum-12-1 vs low-volatility | 0.44 |
| low-volatility vs absolute-trend | 0.26 |

Effective diversification needs correlations below roughly 0.3. One pair of six qualified.
Momentum and short-horizon reversal — nominally opposite signals — correlate at 0.75.

The equal-weight ensemble returned Sharpe 0.82 against 0.84 for its best single component.
It did not help at all. Four components averaging 0.68 Sharpe would reach about 1.36 if
independent; the entire shortfall is correlation.

**Long-only equity strategies cannot diversify each other.** However different the signals
look, they all load on the same equity beta, and that shared factor dominates.

## The fix: separate the risk factor, not the signal

The same 12-1 signal was then confined to asset-class sleeves already inside the universe —
equities, bonds, real assets — with an absolute-momentum filter so a sleeve holds cash
rather than a falling asset.

| Pair | Correlation |
|---|---:|
| sleeve-bonds vs absolute-trend | 0.05 |
| sleeve-bonds vs sleeve-equity | 0.15 |
| sleeve-bonds vs reversal-5d | 0.17 |
| sleeve-real vs absolute-trend | 0.21 |
| sleeve-bonds vs momentum-12-1 | 0.22 |
| *sleeve-equity vs momentum-12-1* | *0.95* |

The last row is the control: two "different" equity strategies are 95% the same thing. The
bond sleeve — correlating 0.05–0.22 with everything else — is the only genuine diversifier,
and it is the worst standalone performer at Sharpe 0.42.

| Ensemble | Sharpe | Max DD | Total |
|---|---:|---:|---:|
| 4 equity-ish strategies | 0.82 | 25.29% | 128.68% |
| 7 including asset-class sleeves | **0.93** | **20.84%** | 142.68% |

Higher Sharpe and lower drawdown, obtained by adding the weakest component. On the holdout
window the equal-weight ensemble returned Sharpe 1.59 with an 8.23% maximum drawdown, 95%
interval **[0.23, 2.94]** — the first result in this project whose interval excludes zero.

## Simulator validation

Cycle 3 used a simplified weight-based simulator, so it was cross-checked against the
production Decimal engine on a strategy both express: 12-1 momentum, top 10, monthly.

| | Production | Simplified |
|---|---:|---:|
| Final equity from $100 | $282.24 | $275.09 |
| Sharpe | 0.772 | 0.793 |
| Max drawdown | 28.12% | 24.64% |

Sharpe difference 0.021, equity ratio 0.97x. **Consistent.** The ensemble result is not a
tooling artifact.

## What this does and does not establish

Established: correlation structure, not signal cleverness, is what determines whether
combining strategies helps. This has a causal mechanism rather than a fitted parameter
behind it, which is the kind of effect that tends to persist.

Not established:

1. **The sleeves were built after observing the correlation failure.** That is iteration on
   data already seen. The sleeves are untuned — fixed 12-1 signal, fixed hold counts, no
   parameter search — so exposure is far lower than in cycles 1 and 2, but this is not a
   pre-registered result and must not be reported as one.
2. **Magnitude remains ordinary.** 142.68% over 9.5 years is about 9.8% annually, doubling
   capital in roughly 7.4 years.
3. **The production configuration cannot express it.** `StrategyConfig` requires SPY in
   every universe and pins both benchmark and regime symbols to SPY, so a bond-only sleeve
   is unrepresentable. Live deployment needs multi-sleeve allocation, which the runtime,
   which runs one strategy over one universe, does not support.

## Cycle 4 pre-registration requirements

Before any of this is believed it needs: the ensemble design fixed in writing in advance;
confirmation of each sleeve in the production engine; a decision rule for sleeve weighting
declared before results; and an honest acknowledgement that no untouched holdout remains in
the historical data — the live paper account is now the only clean out-of-sample source.
