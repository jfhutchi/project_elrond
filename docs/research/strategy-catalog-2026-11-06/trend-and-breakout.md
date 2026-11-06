# Trend, momentum, and breakout candidates — T01–T16

Research date: 2026-11-06. Research only: no strategy, runtime change, backtest, risk setting, or trade has been implemented. Linked sources below were opened during this review. Published evidence and **PROPOSED** tests are separate; an indicator is not evidence of a profitable strategy.

## Common test contract

All choices in a **PROPOSED** paragraph are research hypotheses unless expressly identified as source conventions. They are not author rules or authorized runtime values. Initial scope is long-only eligible liquid stocks/ETFs. Futures, FX, crypto, short selling, borrow, and derivatives require separate extensions. A long-only adaptation does not reproduce a published long-short strategy.

**Sizing S0, used by every candidate:** single-unit/notional-normalized first research pass. For portfolios, equal notional among selected positions, aggregate unlevered exposure at most one normalized portfolio, remainder cash. Single-symbol diagnostics use one unit with returns normalized to entry notional. Later compare volatility-normalized sizing within the existing risk envelope; no new runtime risk values are authorized. These conventions do not replace existing live pilot settings.

Daily signals use completed bars and execute at the next trading session's open, including exits. Monthly signals are determined after the month's last session. Equal ranking scores use ascending permanent security identifier; a top-decile selection takes ceiling(N/10) of N eligible names (proposed rounding convention). Unless stated otherwise, equality retains the previous position. Start flat after complete warm-up; never fill a completed-bar signal retrospectively. “No separate stop” means the stated signal exit is the only strategy exit in this research test, not a proposal to remove runtime controls. Intraday exceptions appear individually.

Use point-in-time eligibility, delisted securities where relevant, corporate-action-consistent OHLC, dividend-inclusive return series for rankings, and actual executable fill prices. Estimate spread, slippage, commissions, turnover, cash returns and missing-bar effects. Freeze rules before chronological out-of-sample tests; compare cash, buy-and-hold and simpler momentum benchmarks. Trade win rate, positive-month fraction and portfolio return are different statistics. **UNKNOWN** means no applicable trade-level rate was verified; no entry establishes a 60–70% win rate.

**Elrond fit:** **A** = reuse daily `BacktestEngine.run` `EXTERNAL_SIGNAL` / `external_weights(date -> symbol -> weight)` accounting after offline rule/weight generation; **B** = new indicator/state adapter or intraday/fill/stop modeling; **C** = new asset, shorting or multileg support. A does not mean implemented. Existing `adaptive_momentum` contains 12-minus-1, MA filtering, Donchian and ATR-stop ingredients. The separate active `paper_core` COLLECT 5m/30min workflow does not implement these candidates.

## Compact index

| ID | Candidate | Distinct decision mechanism | Primary fit |
|---|---|---|---|
| T01 | Time-series momentum | Own past excess-return sign | A; original futures C |
| T02 | Cross-sectional 12-minus-1 momentum | Rank stocks against peers | A |
| T03 | Dual momentum | Relative selection plus absolute cash hurdle | A |
| T04 | Monthly moving-average timing | Price relative to smoothed level | A |
| T05 | Donchian channel breakout | Exceed trailing price extrema | A |
| T06 | Moving-average crossover | Fast versus slow smoothed trend | A |
| T07 | MACD signal crossover | Change in spread between two EMAs | A+B |
| T08 | ADX / directional movement | Directional range dominance with strength gate | A+B |
| T09 | Ichimoku cloud trend | Shifted high-low midpoint cloud confirmation | A+B |
| T10 | Parabolic SAR | Accelerating path-dependent trailing level | A+B |
| T11 | Nearness to 52-week high | Cross-sectional distance to annual high | A |
| T12 | Industry momentum | Rank industry-level returns | A+B |
| T13 | Opening-range breakout | Break current session's initial range | B |
| T14 | First-to-last-half-hour momentum | Early information predicts late-session direction | B |
| T15 | Rising-EMA pullback | Retracement and recovery in established trend | A+B |
| T16 | Bollinger squeeze breakout | Volatility compression then expansion | A+B |

## T01 — Time-series momentum

**Source:** [Moskowitz, Ooi & Pedersen, *Time Series Momentum* (2012)](https://w4.stern.nyu.edu/facdir/lpederse/papers/TimeSeriesMomentum.pdf). Original research finds return persistence over one to twelve months across 58 liquid equity-index, currency, commodity and bond instruments. Its diversified, volatility-scaled long-short futures/forward portfolio differs materially from equity ETFs.

**PROPOSED:** At month-end compute each eligible broad-market ETF's trailing twelve-month total return less compounded Treasury-bill return. Split normalized notional equally across the predeclared ETF universe; hold each ETF only when its difference is positive, otherwise its sleeve stays in cash. Reassess monthly, enter/exit next open, S0, no separate stop. The twelve-month horizon is from the paper; ETF substitution, cash treatment and execution convention are hypotheses.

**Assets/data:** Daily ETF bars aggregated monthly, dividend and cash-return histories. Original replication also needs contract rolls and futures/FX support.

**Evidence/win rate:** Peer-reviewed empirical evidence; proposed ETF trade win rate **UNKNOWN**. Failures: sideways whipsaw, abrupt reversals, delayed rebounds, equity beta mistaken for trend skill. **Fit: A**, offline sign-rule generator; **C** for original instruments/shorts.

## T02 — Cross-sectional 12-minus-1 momentum

**Source:** [Jegadeesh & Titman, *Returns to Buying Winners and Selling Losers* (1993)](https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.1993.tb04702.x). The paper documents positive winner-minus-loser returns over intermediate formation/holding horizons. Its original experiment should not be represented as exactly the modern twelve-minus-one implementation below.

**PROPOSED:** Monthly, rank eligible stocks by total return from month-end twelve months earlier to the preceding month-end, excluding the latest month. Hold the highest decile, equal notional S0, with next-open rebalancing; names leaving the decile exit. Decile selection, one-month holding/rebalancing and skip-month specification are explicit hypotheses. No separate stop or market-trend filter: this isolates relative ranking from T01/T03.

**Assets/data:** Daily/monthly US equities, point-in-time membership, delisting returns, distributions and predeclared liquidity eligibility.

**Evidence/win rate:** Peer-reviewed return evidence; proposed long-only trade win rate **UNKNOWN**. Failures: momentum crashes, sector crowding, microcap costs and survivor bias. **Fit: A**; reuse 12-minus-1 ingredients and external weights, but generate and validate a separate rule definition.

## T03 — Dual relative-plus-absolute momentum

**Source:** [Gary Antonacci, *Dual, Relative & Absolute Momentum*](https://www.optimalmomentum.com/dual-relative-absolute-momentum/). The author combines relative US/ex-US equity selection with an absolute-momentum defensive switch. Displayed index results are hypothetical and explicitly exclude investment/trading fees, not a live performance record.

**PROPOSED:** Monthly rank predeclared US-equity and ex-US-equity ETFs by twelve-month total return. Select the leader only if its return exceeds compounded Treasury-bill return over that window; otherwise hold cash. Execute next open, S0, monthly exit/switch, no separate stop. This winner-specific hurdle and cash fallback are hypotheses, not reproduction of the author's full GEM implementation or bond fallback.

**Assets/data:** Long-only eligible ETFs, distribution histories, common month-end calendar and cash-return series; do not backfill nonexistent ETFs as tradable instruments.

**Evidence/win rate:** Original-author explanation and hypothetical comparisons; proposed rate **UNKNOWN**. Failures: leadership churn, correlated equity losses, delayed reentry and defensive-asset substitution risk. **Fit: A**, new allocation logic feeding external weights. Combination evidence does not make the component momentum exposures independent.

## T04 — Monthly moving-average trend timing

**Source:** [Meb Faber, *A Quantitative Approach to Tactical Asset Allocation*, 2013 update](https://mebfaber.com/wp-content/uploads/2016/05/SSRN-id962461.pdf). The author specifies buying above a ten-month SMA and moving to cash below it, checking month-end. The paper uses signal-day closing fills; those results must not silently become next-open results.

**PROPOSED:** On a dividend-consistent ETF series, compare completed month-end close with the latest ten month-end closes' average. Long above, cash below, next-session open execution, S0; no separate stop. The ten-month lookback is source-specified; ETF mapping and next-open fills are research adaptations. Equality retains state.

**Assets/data:** Daily ETF OHLC, monthly aggregation, distribution and cash accounting. Individual-stock use is a separate unvalidated extension of the asset-allocation evidence.

**Evidence/win rate:** Published practitioner research and historical multi-asset comparisons; proposed trade rate **UNKNOWN**. Failures: sideways crossings, overnight signal-to-fill gaps, missed recoveries and index/ETF differences. **Fit: A**, existing MA ingredients plus monthly external-weight generation.

## T05 — Donchian price-channel breakout

**Source:** [Curtis Faith / Original Turtles, *The Original Turtle Rules*](https://www.tradingblox.com/originalturtles/originalturtlerules.htm). This original participant's publication describes breakout systems, volatility sizing, pyramiding, exits, stops and portfolio constraints. The landing page was accessible; its complete PDF exceeded browser extraction limits. The simplified test is not a full Turtle replication.

**PROPOSED hypotheses:** Enter after a daily close exceeds the highest high of the preceding 55 completed sessions, excluding the signal bar. Exit after a close falls below the preceding 20-session low. Execute next open, S0, no pyramiding or separate stop. Both numerical channel choices are explicitly proposed, not attributed to successfully inspected source text.

**Assets/data:** Eligible stock/ETF daily high-low-close data, split consistency and gap-aware next-open fills.

**Evidence/win rate:** Original-practitioner system provenance; no verified controlled result for this simplified test, **UNKNOWN**. Failures: false breaks, expensive late entries and many small losses between rare trends. **Fit: A**, reusable Donchian ingredients; full futures/Turtle replication requires **B+C**.

## T06 — Fast/slow moving-average crossover

**Source:** [Fidelity, *Simple Moving Average*](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/sma). Official education explains moving-average trend measurement, smoothing/lag and crossing-based signals. This establishes mechanics, not independent evidence of profitability for a selected pair.

**PROPOSED hypotheses:** Compute 50-session and 200-session SMAs on completed closes; enter next open after the fast average crosses from at/below to above the slow average. Exit after the opposite crossing. S0, no separate stop; wait for a fresh crossing after warm-up. Although these horizons are familiar conventions, the exact pair and full trading specification are this test's hypotheses. Comparing two smoothed series differs from T04's price-versus-average rule, although both can have strongly correlated exposure.

**Assets/data:** Eligible stocks/ETFs, full slow-window history, corporate-action-consistent prices and actual open fills.

**Evidence/win rate:** Broker educational evidence only; **UNKNOWN**. Failures: lag, crossing clusters in ranges and late exits in sudden declines. **Fit: A**, SMA calculations and offline crossover-state weights required.

## T07 — MACD signal-line momentum

**Source:** [Fidelity, *Moving Average Convergence/Divergence*](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/macd). Fidelity defines default MACD as twelve-period EMA minus twenty-six-period EMA with a nine-period EMA signal line, describes crossings, and warns of range-market whipsaws.

**PROPOSED:** Daily, buy next open after MACD crosses from at/below to above its signal line; exit after the reverse crossing. Use source 12/26/9 defaults, S0, no separate stop. EMA seeding is an explicit hypothesis: seed each EMA with its period-length SMA, including the signal EMA, and complete all warm-ups before crossings. Add no zero-line condition. This measures changes in a smoothed price spread rather than T06's direct fast/slow trend sign.

**Assets/data:** Eligible stock/ETF adjusted closes and executable opens; sufficient preceding observations for both price EMAs and signal EMA.

**Evidence/win rate:** Official mechanics, no controlled strategy evidence; **UNKNOWN**. Failures: rapid recrossings, lag, initialization differences and overfit confirmation filters. **Fit: A+B**, MACD/state generator into daily external weights.

## T08 — ADX-gated directional movement

**Source:** [Fidelity, *ADX: How Strong Is the Current Trend?*](https://www.fidelity.com/viewpoints/active-investor/average-directional-index-ADX). ADX measures strength, not bullish direction. Fidelity describes above 25 as a strong-trend convention and positive/negative directional lines for direction. Current market commentary is not strategy validation.

**PROPOSED hypotheses:** Use fourteen-session Wilder smoothing for directional movement and ADX. Enter next open when ADX > 25 and +DI > -DI first become true; exit when either fails. S0, no separate stop. The 25 convention is sourced; fourteen sessions, conjunction and exit are proposed. Positive DM is upward high change only when positive and exceeding downward low change; negative DM symmetrically; ties are zero. Seed Wilder averages with arithmetic means. Set zero-range ratios to zero.

**Assets/data:** Eligible stock/ETF daily OHLC with complete smoothing warm-up.

**Evidence/win rate:** Educational mechanics; **UNKNOWN**. Failures: elevated ADX during reversals, threshold churn and inconsistent smoothing. **Fit: A+B**, directional-movement adapter and external weights.

## T09 — Ichimoku cloud-confirmed trend

**Source:** [Schwab thinkorswim, *Ichimoku*](https://toslc.thinkorswim.com/center/reference/Tech-Indicators/studies-library/G-L/Ichimoku). Documentation defines high-low midpoint Tenkan/Kijun, default nine/twenty-six bars, and cloud spans incorporating a fifty-two-bar range, displayed twenty-six bars ahead. Plot displacement supplies no future information.

**PROPOSED:** With source defaults, enter next open when close first exceeds both cloud spans displayed at that date and Tenkan > Kijun. Exit when either price-above-cloud or Tenkan-over-Kijun fails. S0, no separate stop. This conjunction and exit are hypotheses; omit backward-plotted Chikou. At date t, the displayed cloud uses span calculations from t−26, preventing look-ahead leakage. Span A is the mean of Tenkan/Kijun; Span B is the fifty-two-bar high-low midpoint.

**Assets/data:** Eligible daily stocks/ETFs, range-plus-displacement warm-up, consistent OHLC.

**Evidence/win rate:** Broker formulas only; **UNKNOWN**. Failures: lag, sideways churn, future leakage and arbitrary compound filters. **Fit: A+B**, shifted-array indicator/state adapter into daily weights.

## T10 — Parabolic SAR trend following

**Source:** [Fidelity, *Parabolic Stop and Reverse*](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/PSAR). Wilder's described system uses an accelerating trailing level and reverses when hit. Fidelity gives acceleration 0.02, increments 0.02 and maximum 0.2; it warns of sideways-market whipsaws.

**PROPOSED:** Use source acceleration conventions with the deterministic recurrence below, taking only long regimes, S0. After a completed bullish SAR reversal, buy next open; after a completed bearish reversal, exit next open without shorting. No profit target or separate resting stop: SAR is a daily regime-exit signal in this adaptation. Long-only treatment, end-of-bar evaluation and delayed fills are hypotheses and do not reproduce stop-and-reverse execution.

**Assets/data:** Eligible daily stock/ETF OHLC and next-session opens. The recurrence consumes full bar ranges only after close; a breached level is never treated as that bar's achievable fill.

**Evidence/win rate:** Educational system description; **UNKNOWN**. Failures: acceleration overtightening, ranges and overnight delay after breach. **Fit: A+B**, exact SAR state generator into external weights; actual intrabar stop-and-reverse replication needs **B+C**.

## T11 — Nearness-to-52-week-high ranking

**Source:** [George & Hwang, *The 52-Week High and Momentum Investing* (2004)](https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.2004.00695.x). The paper finds annual-high proximity helps explain subsequent returns and adds information relative to past individual and industry returns. This is a ranking/anchoring hypothesis, not a new-high breakout entry.

**PROPOSED hypotheses:** Monthly rank eligible stocks by current split-adjusted close divided by maximum daily split-adjusted high over the latest 252 sessions, including today. Hold the top decile equally, S0, until next monthly rebalance; transact next open, no separate stop. The 252-session mapping, daily-high denominator, decile and holding choice are proposed, not exact paper replication. Apply the common tie rule.

**Assets/data:** Point-in-time stocks, delistings and split-consistent histories; never mix a total-return numerator with unadjusted highs.

**Evidence/win rate:** Peer-reviewed empirical evidence; proposed long-only rate **UNKNOWN**. Failures: crowded leaders, price-adjustment errors and crash reversals. **Fit: A**, new ranking generator; original short portfolios require **C**.

## T12 — Industry momentum rotation

**Source:** [Moskowitz & Grinblatt, *Do Industries Explain Momentum?* (1999), original paper](https://www.andreisimonov.com/Microstr_PhD/MSU_09/MoskowitzGrinblatt99.pdf). Research identifies an industry component in momentum; forming portfolios at industry level differs from ranking individual names. Sector ETF rotation is a proxy, not the paper's historically classified stock portfolios.

**PROPOSED hypotheses:** Freeze eligible, nonoverlapping US industry/sector ETFs available at each date. Monthly rank trailing six-month total returns and allocate S0 equally to the highest three, replacing names next open; no skip month, trend filter or separate stop. If fewer than three instruments have complete history, skip that month. These selection/holding choices are proposed rather than author rules.

**Assets/data:** Long-only ETFs with inception-aware histories; closer replication requires point-in-time industry classifications and constituent-weighted portfolios.

**Evidence/win rate:** Peer-reviewed industry-return evidence; proposed ETF rate **UNKNOWN**. Failures: classification changes, correlated sectors, concentration and ex-post ETF-universe choice. **Fit: A**, **B** for historical classification/portfolio construction; shorts **C**.

## T13 — Opening-range breakout

**Source:** [Zarattini, Barbon & Aziz, *A Profitable Day Trading Strategy for the U.S. Equity Market* (2024)](https://alexandria.unisg.ch/server/api/core/bitstreams/3c2989c4-688d-4d78-8a71-f02690990d51/content). This working paper studies over 7,000 stocks in 2016–2023, including five-minute opening ranges. Its base strategy underperformed buy-and-hold; stronger reported results depended on relative-volume selection, not an unfiltered long-only variant.

**PROPOSED hypotheses:** Record 09:30–09:35 ET high/low. If that interval closes above its open, buy after the first later completed five-minute bar closes above its high, next-bar open. No entries from 15:30. One trade per symbol/day; S0. Stop at opening-range low; exit inventory at 15:55. Confirmation, stop and clock choices differ from the paper.

**Assets/data:** Eligible US stocks/ETFs, calendar, minute/quote data for stops/spread; skip shortened sessions. Resolve ambiguous bars conservatively and gaps at available prices.

**Evidence/win rate:** Original working paper with transfer limitations; applicable aggregate rate **UNKNOWN**. Failures: false breaks, spread spikes, adverse selection. **Fit: B**; shorts **C**.

## T14 — Early-return / late-session momentum

**Source:** [Gao, Han, Li & Zhou, *Intraday Momentum*, October 2014 original version](https://smallake.kr/wp-content/uploads/2015/01/SSRN-id2440866.pdf). Authors find early returns predict the last half-hour for SPY, February 1993–December 2013. Their early return runs from previous close to 10:00 ET, including overnight information, not solely the opening half-hour. Later publication is *Market Intraday Momentum* (2018).

**PROPOSED hypotheses:** If that early return is positive, buy SPY at the 15:30 bar open using information known at 10:00; otherwise flat. Exit at 15:55, S0, no separate stop. Sign rule, long-only restriction and early exit are adaptations, not the authors' regression/portfolio implementation.

**Assets/data:** Regular-session intraday SPY, valid prior close, distributions and exchange calendars; skip shortened sessions. No gap-trading entry or news filter despite the overnight-inclusive feature.

**Evidence/win rate:** Original empirical paper; **UNKNOWN**. Failures: small effects consumed by costs, altered closing flows and predictor-definition drift. **Fit: B**, scheduled intraday execution.

## T15 — Rising-EMA pullback and recovery

**Source:** [Fidelity, *Exponential Moving Average*](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/ema). Education suggests buying dips near/below a rising EMA. That discretionary idea needs operational rules; no accompanying validated win rate was found.

**PROPOSED hypotheses:** Use twenty-session EMA seeded with its first twenty-close SMA. Yesterday must close below its EMA while yesterday's EMA exceeds its value five sessions earlier. Enter next open only if today's close recovers above today's EMA and today's EMA exceeds its five-session-ago value. Exit next open after close below EMA or after ten held sessions, whichever first. S0, no additional stop; require another setup before reentry. All numerical parameters and recovery/exit rules are hypotheses. Ten held sessions means the tenth session close after entry, including entry day.

**Assets/data:** Eligible stock/ETF daily closes, executable opens and full indicator warm-up.

**Evidence/win rate:** Broker concept only; **UNKNOWN**. Failures: failing trends, unrecovered drawdowns and overfit proximity definitions. **Fit: A+B**, setup-state generator; distinct pullback family.

## T16 — Bollinger squeeze expansion breakout

**Sources:** [John Bollinger, *Bollinger Band Rules*](https://www.bollingerbands.com/bollinger-band-rules) and [author-platform screening definitions](https://bollingerbands.us/help-screening.php). Outside-band closes are described as initial continuation signals; defaults are twenty periods/two standard deviations. The platform calls a six-month BandWidth low a squeeze. This follows expansion rather than lower-band reversion.

**PROPOSED hypotheses:** Use source-default bands and population standard deviation as an implementation choice. Yesterday's BandWidth must equal its minimum over 126 sessions; today closes above today's upper band with width greater than yesterday's. Buy next open, S0; exit next open when close falls below middle SMA. No separate stop. The 126-session mapping, immediate-next-day trigger, expansion condition and exit are hypotheses. Width is (upper minus lower)/middle.

**Assets/data:** Eligible stocks/ETFs, daily consistent closes, complete band/squeeze warm-up, actual opens.

**Evidence/win rate:** Original-author mechanics, no validated performance for this specification; **UNKNOWN**. Failures: head fakes, quiet-stock false positives, spread expansion and regime shifts. **Fit: A+B**, band/width state adapter.

## T10 reproducibility appendix — proposed SAR recurrence

The following initialization and tie conventions are hypotheses, not verified details of Wilder's complete original system. Number bars from one. At the second bar's close initialize bullish state only if C2 > C1, otherwise bearish. Bullish initialization: SAR2 = min(L1,L2), EP2 = max(H1,H2); bearish initialization: SAR2 = max(H1,H2), EP2 = min(L1,L2). Set acceleration AF2 = 0.02. Remain flat until the first subsequent bullish reversal.

For each completed bar t >= 3, compute provisional P = SAR[t−1] + AF[t−1] × (EP[t−1] − SAR[t−1]). If previously bullish, clamp P to min(P,L[t−1],L[t−2]). If L[t] < P, reverse bearish: SAR[t] = old EP, EP[t] = L[t], AF[t] = 0.02. Otherwise remain bullish, SAR[t] = P, EP[t] = max(old EP,H[t]); increase AF by 0.02, capped at 0.2, only when H[t] strictly exceeds old EP.

If previously bearish, clamp P to max(P,H[t−1],H[t−2]). If H[t] > P, reverse bullish: SAR[t] = old EP, EP[t] = H[t], AF[t] = 0.02. Otherwise remain bearish, SAR[t] = P, EP[t] = min(old EP,L[t]); increment AF only when L[t] is strictly below old EP. A bar permits one reversal, ties do not reverse, and unchanged EP leaves AF unchanged. Execute resultant long/flat transitions at the next open.

## Review caveats

Return-based momentum has the strongest empirical provenance here; published portfolio returns do not establish net profitability or trade win rates for these simplified tests. Indicator families have weaker evidence. T01/T02/T03 are related exposures; T04/T06/T07 are related trend transforms despite distinct rules. Measure correlation and incremental value rather than treating sixteen entries as independent return sources.

T10 uses an explicitly proposed deterministic SAR approximation and next-open exits, not original intrabar reversal fills. T05 is not full Turtle replication because its PDF could not be extracted. T13's paper uses tighter ATR stops and different sizing. T14 retains the overnight-inclusive predictor but changes execution. Do not conceal those differences in eventual comparisons. NR7 was replaced with directly sourced EMA pullback to cover a distinct family rather than add another compression-breakout variant.
