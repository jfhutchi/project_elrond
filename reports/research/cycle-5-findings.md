# Research Cycle 5 — Exposure Normalisation

Hypotheses and criteria fixed in [cycle-5-hypotheses.md](cycle-5-hypotheses.md) before this
ran. One configuration, one run: the number of trials is one.

## Result

| Series | Capped (cycle 4) | Normalised (cycle 5) | Sharpe 4 → 5 | Max DD 4 → 5 |
|---|---:|---:|---:|---:|
| equity sleeve | $144.11 | $156.53 | 0.69 → 0.63 | 9.19% → 11.12% |
| bond sleeve | $104.74 | $114.14 | 0.31 → 0.33 | 4.96% → 13.82% |
| real-asset sleeve | $114.76 | $127.88 | 0.62 → 0.64 | 4.47% → 6.97% |
| **ensemble** | $120.51 | **$132.78** | **0.82 → 0.80** | 3.93% → 6.06% |
| SPY buy & hold | $454.70 | $454.70 | 0.90 | 33.79% |

## All three hypotheses confirmed

**H1 — returns rise.** The ensemble improved from $120.51 to $132.78, and every sleeve rose.
Deploying more capital through the same signal does produce more return.

**H2 — Sharpe does not improve.** 0.82 → 0.80. It fell marginally. This is the predicted
result and the reason the cycle was worth running: holding a fraction *f* in assets and the
rest in cash yields *f* × asset returns, so mean and standard deviation scale together and
the ratio is invariant. The engine behaves as the arithmetic requires.

**H3 — the drawdown advantage was cash, not skill.** The ensemble's maximum drawdown rose
from 3.93% to 6.06%, and the bond sleeve's nearly tripled from 4.96% to 13.82%. Cycle 4's
apparently excellent risk profile was substantially an artifact of being under-invested.

## Verdict

Criteria 1–4 pass, criterion 5 fails: ensemble Sharpe 0.80 against SPY's 0.90, versus 0.82
against 0.90 before. **DO NOT PROMOTE.**

Per the interpretation fixed in advance: criterion 5 failing with Sharpe roughly unchanged
means the shortfall is **structural, not a sizing artifact**. No exposure or position-cap
adjustment can make this strategy family beat the benchmark on risk-adjusted return. That
avenue is now closed rather than left open as a plausible fix.

## Where this leaves the strategy family

Five cycles, 10.6 years, two bear markets, 31+ candidates, and every promotion criterion set
before results were seen. Summary of what was eliminated:

| Hypothesis | Outcome |
|---|---|
| The shipped component stack works | Refuted — components fight each other, two are inert |
| A better parameter exists | Refuted — the best was knife-edge at one exact value |
| A better component combination exists | Refuted — best design failed its robustness check |
| Signal-family ensembles diversify | Refuted — long-only equity strategies correlate 0.44–0.75 |
| Asset-class sleeves diversify | **Confirmed** — correlations 0.05–0.22, real Sharpe benefit |
| The ensemble beats the benchmark | Refuted — 0.82 vs 0.90 |
| Exposure was the missing ingredient | Refuted — Sharpe invariant to exposure |

One durable positive finding across five cycles: correlation structure, not signal design,
determines whether combining strategies helps. It reproduces in the production engine and
has a mechanism rather than a fitted parameter behind it.

**Recommendation: stop developing this strategy family.** The remaining honest options are a
different asset class with an independent evidence base, or accepting the measured result
that the benchmark outperformed everything constructed here. Neither is reached by another
cycle on US equity ETF momentum.
