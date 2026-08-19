# Tested and Refuted

Single authoritative index of every hypothesis tested, so no session re-tests a dead idea or
trusts a stale summary. Read this file **in full** before proposing research. It is short by
design; if it stops being short, split it rather than skimming it.

Rule: nothing is listed here without a measurement behind it. "It seemed unlikely" is not a
refutation.

Last updated 2026-08-18 (cycles 11-17).

## Refuted with measurement

| # | Hypothesis | Verdict | Evidence |
|---|---|---|---|
| 1 | Momentum parameter tuning finds an edge | REFUTED | Cycle 1-2. 31 candidates over 10.6y. Best (D7) beat SPY on holdout Sharpe 1.22 v 1.15, then failed parameter robustness — edge existed only at `momentum_long=252` exactly |
| 2 | The full shipped strategy is worth running | REFUTED | Ranks **6 of 6**. CAGR 0.50%, Sharpe 0.15, $105.41 from $100 over 10.6y. SPY buy-and-hold: $454.70 |
| 3 | Signal-family ensembles help | REFUTED | Cycle 3. Component signals correlate 0.75+; no diversification available |
| 4 | Exposure normalisation helps | REFUTED | Cycle 5. Sharpe is scale-invariant; the ensemble's low drawdown was under-risk, not skill |
| 5 | Crypto momentum | REFUTED | Cycle 5. −79.9%, Sharpe −0.45, 80.6% drawdown |
| 6 | BTC trend standalone | REFUTED | Cycle 5. Sharpe 0.53, 95% CI [−0.55, 1.61] spans zero |
| 7 | BTC as a portfolio diversifier | REFUTED | Correlation is genuinely low (+0.152) and blend improvement is **0.23 sigma, p≈0.82**, inside the luck band for 7 weight trials |
| 8 | Shorter horizons | REFUTED | 1-day reversal has the **strongest raw edge measured** (+427.9% gross) and nets −28.8% at 5bps. Never beats the slow signal at any cost level, including an unachievable 0.5bps |
| 9 | Options as a route to faster gains | REFUTED | Affordable contracts at $100 are 0DTE at **10–18% spreads** (1,000–1,800bps). The 5bps that killed #8 applies here 200–370x harder |
| 10 | Machine learning / meta-labelling | REFUTED | Cycle 8. Looked strong (PF 2.29 v 1.06) and failed: n=39, t=1.72, p=0.085, CI [−0.104%, +1.598%] spans zero, luck threshold for 43 cumulative trials is 2.22 |
| 11 | Energy is more predictable via geopolitics | REFUTED | Cycle 9. Energy trend **persistence 21.2d < equities 26.5d** — trends are shorter, not longer. −22.0% return, 78.3% drawdown. Geopolitical events are shocks; shocks mean-revert |
| 12 | FX diversification | REFUTED | Cycle 9. Lowest correlation in the study (0.02) and unusable — Sharpe −0.14. A losing stream cannot diversify |
| 13 | Reddit / social sentiment | REFUTED (literature) | Long-minus-short WSB portfolios produce alpha indistinguishable from zero; higher return with worse Sharpe |
| 14 | Coinbase for paper trading | IMPOSSIBLE | Advanced Trade sandbox returns static fixtures, no P&L tracking. Coinbase for Agents is **real money only**, explicitly no paper/testnet |
| 15 | Cross-sectional breadth (whole-market momentum) | REFUTED | Cycle 10, **survivorship-free**: 511 names incl. 241 delisted. Sharpe 0.63 v SPY 0.80, −0.54 sigma, p≈0.59. Higher raw return, worse risk-adjusted |
| 16 | Volatility targeting as an **alpha** source | REFUTED | Cycle 11. Looked like the best result yet (Sharpe 0.92→1.09, all 12 parameter sets winning, break-even 113x real cost) and died: Jobson-Korkie/Memmel **z=1.01, p=0.31**, wins 6 of 12 assets, loses 2025-26. See #S2 for what did survive |
| 17 | Overnight (close-to-open) premium | REFUTED | Cycle 11. An error in this project's own batch-2 analysis inflated it: one-sample t-tests instead of **paired**. Corrected, SPY falls t=2.74→0.63 and 1 of 18 assets clears the bar. Overnight-only beats buy-and-hold on CAGR on **1 of 18** — buy-and-hold collects both sessions. SPY premium decayed 7.93%/yr (2016-18) → −0.44% (2022-24) |
| 18 | Turn-of-month effect | REFUTED | Cycle 11. Positive on 4 of 7 assets, largest t=1.57 (GLD) against a 2.82 bar |
| 24 | Momentum sorts GLOBAL country indices | REFUTED | Cycle 17, asked by Hutch and the right question to ask — country indices have far wider dispersion than US sectors, which was the standing excuse for why the ranking failed. 22 indices (Japan, Germany, UK, Brazil, India, China, Korea, Taiwan, Australia, Canada, Mexico, South Africa, Israel, Turkey, Poland...) 2016-2026. Top third 6.54% CAGR, bottom third **8.96%** — backwards again. Top minus bottom **-2.47%/yr, t=-0.78** against a 2.90 bar |
| 23 | Momentum sorts individual stocks, even though it fails on ETFs | REFUTED | Cycle 16, survivorship-free: 595 names, 260 of which stopped trading. The ranking sorts **backwards** — bottom decile 36.04% CAGR against top decile 4.46%, t=-2.08 against a 2.89 bar. It is entirely a low-price artefact: at a $10 floor the spread halves (t=-1.32) and at a **$20 floor it vanishes exactly** (top 8.07% v bottom 8.10%, **t=0.00**). At 200bps, realistic for distressed small caps, every arm is negative |
| 22 | **The momentum ranking carries information at all** | REFUTED | Cycle 15, and this one subsumes most of the others. Alpha against SPY is **0.10%/yr, t=0.05**. Selecting the top 10 beats holding all 23 by 1.43%/yr at **t=0.84** against a 2.87 bar. Top-decile minus bottom-decile is **t=0.77** — the ranking does not separate winners from losers. The rotation is beta 0.71 with no alpha, and SPY held at 0.71x reproduces it to within **0.05 CAGR points** with no trading at all |
| 21 | Weight-ramping is a cheap substitute for tranching | REFUTED | Cycle 13. Ramping toward target over 5 or 10 sessions changed outcome dispersion by **-9%** and **-6%** - slightly worse than jumping. Dispersion comes from *which date the ranking is evaluated on*, not transition speed. Rules out the only non-invasive implementation |
| 20 | Per-position vol targeting makes more money than flat weights | REFUTED | Cycle 13. At matched 1.0x exposure with the halt enforced at a realistic 21-day resumption it gains $15.88/$100, and the bootstrap 95% CI is **[−$86.76, +$76.79]**, losing in **36.8%** of resamples. Its drawdown reduction is established (#S2); its wealth effect is not |
| 19 | Any strategy here beats SPY at its own growth-optimal leverage | REFUTED | Cycle 12. Vol-targeted SPY at 4.25x appeared to turn $100 into $2,475 v SPY's $954. Stationary bootstrap (2000 draws, 21d blocks): 95% CI on the gap **[−$173.90, +$432.04]**, spans zero; loses in **22.8%** of resamples. Rests on the Sharpe difference already refuted as #16 — leverage amplifies an edge that is not there |

## Four of the above were never actually tested

Added 2026-08-19. **The table above is unchanged and stands as recorded.** This is an audit
beside it, not a rewrite: `reports/research/refuted-power-audit-2026-08-19.md`.

Where an entry reports its own 95% interval, the standard error follows (`width / 3.92`) and so
does the smallest effect the study could have detected (`2.9 x SE`). Four entries measured an
effect **smaller than their own detection limit**, which is the signature of an instrument too
blunt to see what it was pointed at:

| # | hypothesis | could only detect | measured | verdict |
|---|---|---:|---:|---|
| 6 | BTC trend standalone | 1.60 Sharpe | 0.53 | **UNDERPOWERED** |
| 10 | ML / meta-labelling | 1.26% | ~0.75% | **UNDERPOWERED** |
| 19 | growth-optimal leverage beats SPY | $448 | $129 | **UNDERPOWERED** |
| 20 | per-position vol targeting | $121 | $15.88 | **UNDERPOWERED** |

A confidence interval spanning zero was treated as the reason for refutation in several of
these. An underpowered study *always* produces an interval spanning zero, so that observation
carries no information about the mechanism.

**These three mechanisms are untested, not dead:** BTC trend, per-position vol targeting,
meta-labelling. That does not mean re-run them — the window they need is already exhausted, and
a re-run would reproduce the same silence. It means they become live questions again if the data
improves.

**#22 stands, but on its replication rather than its statistic.** Its t-test could not have
detected an alpha below 5.74%/yr, so "no alpha" is not what the test showed. The entry's other
argument — SPY held at 0.71x reproduces the rotation to within 0.05 CAGR points with no trading
— is an equivalence demonstration that does not depend on power, and it is decisive on its own.

For the remaining entries power is not recoverable, because no expected effect size was ever
pre-registered. That is exactly the field #5 now requires and #19 now gates on, so the ambiguity
ends with this corpus.

The honest summary of the whole table is therefore **"nothing large was found"** rather than
"these mechanisms do not work" — weaker, and accurate.

## Not refuted — measured as genuinely working

| Mechanism | Effect | Cost |
|---|---|---|
| SPY buy-and-hold | 15.36% CAGR, Sharpe 0.90, $454.70 per $100 over 10.6y | 33.79% drawdown. **Beat everything built here** |
| SPY 200-day trend | 10.34% CAGR, **Sharpe 0.91** (highest measured) | 19.50% drawdown |
| Leverage on a high-Sharpe strategy | 2x SPY: 21.48% CAGR, doubles in 3.6y v 4.9y | 58.8% drawdown; breaches the 20% halt; Reg T liquidates before recovery |
| More capital | Perfectly linear — verified $100/$1k/$10k give identical CAGR and Sharpe | Requires capital |
| More time | Compounding | Requires time |
| **Tranching the rebalance date** | Removes **2.31 CAGR points** of dispersion driven by nothing but which day of the month the rotation runs (best offset 11.20%, worst 8.89%, identical strategy) | Cycle 11. Converts a gamble to its expected value; does not raise the mean |
| **Vol targeting as drawdown control** | Max drawdown reduced on **16 of 18 assets, p=0.0007**. SPY 33.8% → 20.1% | Cycle 11. Costs a median **1.83 CAGR points/yr**. Not alpha — see refuted #16 |
| **Real execution cost is ~1bp, not 5bps** | Measured NBBO: SPY 0.26, QQQ 0.41, IWM 0.66, median ~1.1bps | Cycle 11. Does not revive #8, already tested at 0.5bps. Means the deployed cost model is miscalibrated |
| **Fast halt resumption** | Delay is the whole cost of the halt: 5d $479, 21d $413, 63d $364 per $100 | Cycle 11. Direction is robust; the 5d row beating no-halt is one path and inside noise |
| **Vol targeting as constraint relief** | Under the 20% drawdown rule it carries 0.75x v SPY's 0.50x and ends at **$403.67 v $281.02, +44%** | Cycle 12. This is the honest form of the vol-targeting claim: not alpha (#16), but more exposure affordable under a fixed drawdown cap |
| **SPY 200-day trend, and nothing else** | 10.34% CAGR, **Sharpe 0.91 — the highest measured in this project**, 19.5% drawdown, 77% average exposure | Cycle 16 re-confirmed it in the production engine. Beats the hand-built index config tenfold (0.98% CAGR) using none of its machinery. Its 19.5% drawdown clears the 20% liquidation threshold but not the 15% entry halt |
| **An interior growth optimum exists** | SPY peaks at 3.25x ($954/$100) with margin charged at 5.75%; free borrowing puts it past 5x | Cycle 12. Implies a **79.2% drawdown** and is unreachable. Constant leverage above ~3.5x goes to **zero** |

Leverage helps a *good* strategy only. At 2x it added 0.75% CAGR to momentum+trend and was
worse than unlevered at 3x; it turned the sleeve ensemble from +1.78% to **−2.49%**.

## Structural limits that bound all future work

* **Span, not sample size, is what buys statistical power — so the exhausted equity window is
  the *weakest* data available, not the best.** The limit above was always stated in years, and
  that is not an accident of phrasing: for an annualised Sharpe the sampling frequency cancels,
  because `SE ~= sqrt((1 + SR^2/2) / years)`. A monthly series over 35 years therefore resolves
  a **smaller** effect than a daily series over 10.6, despite having a twentieth of the rows.
  Measured with `scripts/power_survey.py` at the current 68-trial bar, charging realistic
  autocorrelation:

  | dataset | years | minimum detectable Sharpe |
  |---|---:|---:|
  | SIP US equities, daily (EXHAUSTED) | 10.6 | **0.94** |
  | FRED monthly, PAYEMS/UNRATE | 35 | 0.67 |
  | FRED daily, T10Y2Y from 1976 | 50 | 0.56 |
  | FRED monthly, CPI from 1947 | 75 | **0.46** |

  SPY buy-and-hold scores 0.90 on the equity window — i.e. sitting *at* its own detection limit,
  which is why nothing measured there could ever separate from noise. Long free macro history is
  roughly twice the resolution, and it is not a consolation prize for having run out of
  equities. This was mis-stated in the opposite direction in an earlier session note; the
  arithmetic above is the correction.

* **Statistical.** Separating a true Sharpe of 1.0 from zero needs ~3.8 years of daily data;
  0.5 from zero needs ~15 years. Resolving the D7-vs-SPY gap of 0.074 would need **~700
  years**. Most differences this project can measure are inside the noise.
* **Multiple testing.** ~43 candidate evaluations against the same dataset raise the
  expected-best-by-luck threshold to t≈2.22. Every new result must be deflated by the
  cumulative count, not this cycle's count.
* **Holdout exhausted.** Cycles 2-7 consumed every out-of-sample window and SIP data begins
  2016-01-04. **The live paper account is now the only uncontaminated data this project will
  ever get.**
* **Breadth does not rescue it.** Grinold's law predicts information ratio scales with
  the square root of breadth, so ranking hundreds of names should beat ranking 23 ETFs.
  Tested without survivorship bias, it produced more return and less Sharpe. Alpaca
  lists 19,194 delisted US equities against 14,233 live, and serves history for the
  delisted ones that terminates at each delisting date — so this bias is avoidable and
  was avoided.
* **Momentum fails on single stocks too, and the apparent reversal is a price artefact.**
  Cycle 15 left open that 23 correlated ETFs might simply have nothing to sort. Cycle 16 ran
  the same test on 595 survivorship-free single names where the literature says the effect
  lives. The ranking sorts backwards, and the effect dies **exactly** at a $20 price floor
  (t=0.00) and dies on cost below it. A result that disappears when penny stocks are excluded
  was never a signal.
* **The strategy engine cannot express the only rule that beats the market.** SPY_SMA200
  (10.34% CAGR, Sharpe 0.91, 77% exposure) is the highest-Sharpe mechanism measured here. Two
  configs tried to reproduce it — `strategy-index-v3.yaml` reached 30% exposure and 0.98%
  CAGR, `strategy-trend-v4.yaml` with stops effectively disabled reached 43% and 2.52%. The
  cause is monthly rebalance granularity, not sizing: removing the ATR risk cap entirely via
  `target_weight_sizing` changed nothing. The engine is invested on 43% of sessions against the
  benchmark's 77% using the same 200-day condition, because it only acts at month end and so
  misses every mid-month re-entry. The fix is a per-session evaluation path. Cycle 16.
* **The momentum ranking carries no information in ANY universe tested.** Three independent
  structures, three nulls, and this is the strongest form of the finding:

  | universe | top - bottom | t | bar |
  |---|---|---|---|
  | 23 US sector/asset ETFs | +2.60%/yr | 0.77 | 2.87 |
  | 595 US single stocks (survivorship-free) | -32.19%/yr | -2.08 | 2.89 |
  | 22 global country indices | -2.47%/yr | -0.78 | 2.90 |

  The US-ETF null had a standing excuse — correlated slices of one market, nothing to sort.
  Single stocks killed that, and country indices kill it again with the widest dispersion
  available. Caveat worth keeping: these are US-listed ETFs, so currency exposure is unhedged
  and local small caps are absent. A test on native exchanges could differ but Alpaca cannot
  reach them.
* **A halt that removes all exposure cannot be escaped by any drawdown-based rule.** At zero
  exposure equity is frozen, so the drawdown is frozen, so hysteresis, resume thresholds, and
  every other release condition expressed in terms of drawdown are structurally incapable of
  firing — not mistuned. Measured byte-identical to the broken policy at resume levels of 10%,
  5% and 1%. Only keeping some exposure, or ageing the high-water mark out on a rolling window,
  can release it. Cycle 18, `scripts/halt_policy_study.py`.
* **Releasing the halt does not make a worthless strategy profitable — it exposes it.** The hard
  halt scored Sharpe 0.36 on `strategy-trend-v4` by blocking entry on 65.6% of sessions. With a
  2500bps floor it trades 25 times instead of 6, and scores 0.21 at double the drawdown. A halt
  that suppresses trading will always flatter a strategy with no edge. Cycle 18.
* **There was never a signal to tune.** Cycle 15 tested the premise underneath cycles 1-10 and
  it fails: the momentum ranking does not separate winners from losers (top-minus-bottom
  t=0.77), selection does not beat not-selecting (t=0.84), and alpha against SPY is 0.10%/yr
  at t=0.05. The deployed rotation is beta 0.71 with zero alpha, and a single fractional SPY
  position reproduces it to within 0.05 CAGR points. **This explains every earlier null result:
  ten cycles were tuning the parameters of a signal that carries no information.** It should
  have been the first test run, not the fifteenth.
* **The deployed configuration is the worst available option under the real risk rules.**
  Sized to the 15% entry halt: SPY+vol-target $288.33, rotation+vol-target $203.92, SPY
  buy-and-hold $178.06, deployed rotation $175.99. Bootstrap CI on the best-versus-deployed gap
  is [-$25.08, +$341.56], spanning zero but losing in only 5.3% of resamples — suggestive, not
  established. Cycle 14.
* **Realising a gain does not compound it.** Position size is a percentage of equity, so a
  gain already enlarges the next position while the position is simply held. Selling and
  rebuying produces identical exposure minus the spread: round-tripping SPY daily for a year
  costs 0.66% and buys nothing. The snowball is automatic and already running; what varies is
  only how often capital is *reallocated*, which is a separate question from realisation.
* **More trades do compound faster, but cost scales with certainty and edge only on average.**
  Break-even per trade is fixed at the spread (0.26bps SPY, ~1.1bps universe median), but
  annual drag scales with frequency: 252 trades/yr costs 0.66-2.77%, 5,040 costs 13.1-55.4%,
  25,200 costs 65-277%. Doubling every 3 days needs 25.99% net per session — 1.16% net per
  trade even at 20 trades a session. The strongest per-trade edge this project ever measured
  (#8, +427.9% gross) nets -28.8% at 5bps. `scripts/snowball_math.py`.
* **Execution cost, not prediction, is usually binding.** #8 and #9 both died on spread, not
  on signal quality.
* **The deployed strategy violates its own risk rule.** V1.2.0's historical max drawdown is
  26.4% against a 20% halt, so it is expected to trip the halt it ships with. This is a design
  incoherence, not a returns question, and is the sole justification for v1.3.0. Cycle 13.
* **Tranching is real, measured, and needs 5 tranches at $100.** The $61.24 spread per $100 is
  the largest free effect found in 13 cycles. 5 tranches removes 88% of it at a $2.00 minimum
  trade; 21 would remove 100% but needs $0.48 trades, below Alpaca's $1 floor. The binding
  constraint is capital, not strategy, and relaxes above ~$210. See `docs/tranching-spec.md`.
* **A plausible feature can be a silent no-op.** Per-position vol targeting at a 15% target
  never binds against a 10% weight cap — it would need 150% annualised volatility, which these
  ETFs never reach. The first implementation produced byte-identical results to no feature at
  all and only a sweep revealed it. Cycle 13.
* **The 20% drawdown halt is the true limit on wealth, not the signal.** SPY buy-and-hold draws
  down 33.8%, so the project's own rule forbids holding the benchmark that beats everything
  built here. Every strategy must be sized down to fit, and that sizing decision dominates
  every signal question cycles 1-11 asked. Cycle 12.
* **Leverage above 2x is unavailable regardless of the mathematics.** Reg T caps initial
  leverage at 2x for equities; the drawdown rule caps it near 1x; and constant leverage above
  ~3.5x is not merely worse but bankrupt. Cycle 12.

## Defects found in this project's own analysis

Listed because the error rate matters when judging any result here. Eight found in one day:
two whole-share decrement loops that silently zeroed four backtest variants; a decimal
associativity bug tripping the trade P&L invariant; a stale-lock misdiagnosis; a duplicated
feature vector omitting momentum; a verdict function declaring success with no significance
test; a paginated API response misread as missing data; and a false claim that the daemon was
running when it had never started.

Cycles 11-12 added six: a non-reentrant lock that meant the daemon could never have run a
cycle; unfilled orders consuming no risk budget, which double-spent capital and took a broker
403; a Corwin-Schultz spread estimate wrong by two orders of magnitude, caught only by pulling
real quotes; and **an unpaired t-test that made the overnight effect look significant when it
is not**; and two in one leverage function — debt held fixed instead of leverage, so high
leverage looked survivable; then a monthly-rebalanced margin path compared against a
daily-rebalanced baseline, which appeared to show that margin calls *create* wealth.

This file is now also loaded into structured research memory (#6) by
`quantbot.research.memory.import_refuted_markdown`, verbatim. The records are additional to this
file, not a replacement for it: every statement is carried as written, and the import is
idempotent. Edit here; the records follow.

A thirteenth, and it sat at the top of `STATUS.md` where every session reads it first. The
severity-1 block attributed **70.8% halted sessions** and a **26.4% drawdown** to the deployed
`strategy-v1-2.yaml`. Measured through the production engine on SIP: **0 halted sessions and
9.09% max drawdown**. The 70.8% belongs to `strategy-trend-v4`; the 26.4% does not reproduce on
any window tried. Both figures came from the era of the disconnected harness, and the attribution
drifted from the config that produced them to the config being discussed two sentences later.
The underlying design defect is real and unchanged — the halt genuinely has no exit — but it is
latent rather than active, which is a different decision. Unlike most entries here this one did
not flatter a result; it overstated a risk, which is the rarer direction and still worth the same
scepticism.

A twelfth, found the same way as the tenth -- by a test rather than by review. The first
version of the FRED provider (#17) added each series' publication lag to its *stamped* date, but
FRED stamps a monthly series with its period **start**. March payrolls therefore appeared
available on 6 March, five days before March had ended. Ordinary look-ahead, invisible in any
result it would have produced: it would simply have made the strategy look prescient. The lag now
runs from period end, and the case is pinned. No result was affected -- the provider had never
been run.

The harness-not-connected defect below is now mechanically guarded.
`switches_for_config()` maps a configuration's components onto the engine's switches, and
`test_changing_the_config_changes_the_result` fails if a harness produces identical output for
different configs. The three results it produced remain unattributed until re-measured; the
guard stops a fourth.

An eleventh, also from this session and also found by a guard rather than by review: `cli.py`
imported research code at module scope, so a failure anywhere in the research package would have
broken the kill switch, which lives in the same file. Found by the #14 import-graph test on its
first run. No incident resulted; it is recorded because the kill switch is the last control and
it had acquired a dependency nobody chose.

A tenth defect, found in this session and in this project's own test suite rather than in its
analysis: the first version of the point-in-time feature test (#17) passed with the
slowest-input rule deleted. Its late-published input was still not the *latest available* one,
so replacing "gate on the slowest availability" with "gate on the newest observation" produced
the same answer and the test stayed green. Caught by mutation-testing the guard rather than by
reading it. The same shape as the #12 memory-limit defect that `CLAUDE.md` now carries a rule
about, and the reason that rule is worth the words.

The defects in this section are now partly mechanical. #18 turned four of them into invariants
that run on every result bundle: a statistical test that does not match the dependence structure
of its data, costs that improve a return, forced liquidation that ends richer than no
liquidation, and a significance claim below the luck bar frozen for its own trial count. The
harness-not-connected failure — three of the entries below — is caught by a bundle comparison
that flags identical results across differing inputs.

Found while building the migration path for #5 and #19: **three of the four Alembic revisions
built their tables from live metadata rather than from their own definitions**, so each stopped
describing the schema it actually produced as soon as the next revision landed. Revision 0002
created V3 column names on a fresh database and 0003's rename then failed on a column that had
never existed. A second instance in the same pair of revisions left a backfill `server_default`
attached, which diverged the migrated schema from head metadata. Both were caught by the
post-upgrade schema comparison rather than by reading the code -- the same asymmetry as every
entry below. Each revision now names its own tables. Neither flattered a research result; they
are recorded because the migration path is what will carry every future result.

Found while building the hypothesis registry (#5): **this project has used two different
luck bars.** Cycle 10 quotes t=2.22 for 43 trials, which is Bailey & Lopez de Prado's finite-N
expected maximum. Cycles 15-17 quote 2.87/2.89/2.90, which is `sqrt(2 ln N)` — about 0.5 higher
at comparable N. Bars quoted before cycle 15 are therefore not comparable with bars quoted
after it. **No verdict changes:** every refuted result failed by a wide margin under either
convention (cycle 8's metalabelling t=1.72 misses both 2.22 and 2.74). The registry uses the
stricter `sqrt(2 ln N)` going forward and does not retro-fit either historical number. Unlike
the defects below, this one did not flatter a result — it is recorded because a bar that moves
silently is the mechanism by which one eventually would.

**Every one of these errors flattered the result.** None was caught by the code looking wrong;
each was caught by the number being implausible. That asymmetry is the single most useful thing
to know when reading any figure in this repository.

Every apparent winner in this project evaporated under a test. That is the strongest single
finding here.
