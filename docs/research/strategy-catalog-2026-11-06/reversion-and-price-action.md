# Reversion and price-action strategy catalog — 2026-11-06

Research candidates R01–R16; no implementation, execution, backtest, or allocation recommendation. All numeric rules under **Proposed test** are research operationalizations, not claims about the cited author's complete strategy. Parameter changes and bullish/bearish mirrors belong to their parent candidate, not additional strategies.

## Shared research conventions

- **Sizing S:** start with one-unit trade diagnostics and equal-notional normalized portfolio comparisons. A later volatility-normalized comparison must remain inside the already approved risk budget. No numerical risk allowance, leverage authorization, averaging down, or runtime change is implied.
- **Daily execution D:** use split-consistent OHLC and separately correct total-return accounting; signals use completed bars only, with entry/exit at the next available session open plus realistic costs. All daily stops and targets below are **close-triggered**, executed next open; they do not guarantee an intraday price. Time exits trigger after the stated number of held sessions. ATR means 14-session Wilder ATR, frozen at signal unless explicitly stated. Stops/targets specified relative to entry use actual simulated entry. Skip a setup if its opening entry is already beyond its stop or target. One position per symbol; no pyramiding.
- **Intraday execution I:** liquid equities/ETFs, regular exchange sessions with holidays, half-days and timezone handling; 5-minute completed bars, next-bar open plus spread/slippage/fees. No entry after 14:00 exchange-local time; flatten at a prespecified executable bar before session close (15:55 for a normal US session). Include halts, delayed/missing bars and opening-auction effects. Bar highs/lows do not establish a favorable tick fill. Intraday targets/stops below trigger on completed-bar closes unless explicitly modeled otherwise.
- **VWAP definition:** cumulative sum of trade price × traded volume divided by traded volume, reset each regular session. OHLCV approximation uses typical price `(H+L+C)/3`; label it an approximation. Its deviation `s` is the volume-weighted standard deviation of completed typical prices since session open. Real validation needs full-session traded volume and quotes/costs; FX tick counts are not consolidated traded volume.
- **Elrond compatibility:** **A** = offline daily rule generation can supply date-to-symbol target weights to the discovered `BacktestEngine.run(EXTERNAL_SIGNAL, external_weights=...)`, retaining next-session execution and costs. This is scaffolding, not an already implemented strategy or universal plug-in. **B** = rule/execution/data extension required, especially intraday. A-to-B changes such as intraday stops must be tested separately. The daily `adaptiveMomentum` research path is distinct from active `paper_core` in COLLECT mode on 5-minute bars with roughly 30 minutes collected; that collection does not validate these strategies.
- **Validation:** all proposed win rates are **UNKNOWN**. Report net expectancy, payoff ratio, drawdown/tail loss, turnover, exposure, capacity and uncertainty alongside hit rate. Lock definitions before testing; use chronological out-of-sample periods, delisting-aware universes where relevant, overlapping-signal controls, and simple buy-and-hold/time-matched baselines. A high hit rate can coexist with negative expected profit.

## R01 — RSI(2) pullback in an established uptrend

**Source/evidence:** [Larry Connors' public RSI research](https://tradingmarkets.com/recent/why_rsi_may_be_one_of_the_best_short-term_indicators_-673367) examines very short RSI extremes and a 200-day trend filter. Author research, but commercially presented and historical, not independent replication. His separate [Improved R2](https://tradingmarkets.com/recent/the_improved_r2_strategy_84_correct_with_just_6_rules_-674361) reports 84.31% correct for 102 simulated S&P 500 trades, January 1995–December 2006, using a three-day declining RSI(2) sequence starting below 65, an above-200-day filter, close entry and RSI-above-75 exit. That result does **not** establish this candidate's win rate or executable net return.

**Proposed test:** daily close above SMA200 and Wilder RSI(2)<5; enter using D. Exit after a close above SMA5, a close below entry minus 2 ATR, or five held sessions. Size S. Thresholds 2/5/10 are sensitivity variants, not separate candidates.

**Assets/data:** liquid equity ETFs and survivorship-aware equities; 200-session warm-up. **Win rate:** UNKNOWN. **Failures:** persistent selloffs, overnight gaps and tiny gains overwhelmed by costs. **Elrond:** A after offline rule generation; close-based stop differs from any intraday ATR protection.

## R02 — Bollinger lower-band reentry reversal

**Source/evidence:** [John Bollinger's rules](https://www.bollingerbands.com/bollinger-band-rules) define relative extremes and default 20-period/two-standard-deviation bands, while explicitly warning that tags are not trade signals and outside closes can indicate continuation. This is first-party indicator guidance, not evidence for a profitable reentry system.

**Proposed test:** calculate SMA20 and population standard deviation of the last 20 closes. Require yesterday's close below its lower band and today's close back above today's lower band but below SMA20. Buy next open. Exit on a close at/above the current SMA20, below entry minus 2 ATR, or after ten sessions. Use S and D. Do not enter if the opening price is already above the current signal-day midpoint. Upper-band short mirrors and band-width changes stay in this family.

**Assets/data:** daily liquid ETFs/equities, at least 20 bars plus ATR warm-up. **Win rate:** UNKNOWN. **Failures:** trend persistence, changing volatility, gap losses and overly close profit targets. **Elrond:** A; no claim that the existing Bollinger indicator implements these rules.

## R03 — Intraday VWAP deviation reversal

**Source/evidence:** [Schwab's VWAP education](https://www.schwab.com/learn/story/how-to-use-volume-weighted-indicators-trading) explains session volume-weighted prices and their use as an intraday reference. It supplies indicator mechanics and interpretation, not a validated deviation-fade edge or hit rate. Mean reversion here is a hypothesis.

**Proposed test:** after the first 30 minutes, a completed close falls below VWAP−2s; within the next three bars a close crosses back above VWAP−2s while remaining below VWAP. Buy next bar. Freeze ATR14 from completed 5-minute bars at signal; stop on a close below entry−1.5 ATR. Exit on the first close at/above current session VWAP, after twelve held bars, or the I session cutoff. Use S; only one attempt per symbol/session. No setup when s=0 or volume is missing.

**Assets/data:** liquid equities/ETFs; full sessions, actual volume and quote-informed costs. **Win rate:** UNKNOWN. **Failures:** directional days, stale VWAP, opening noise and many small wins versus trend-day losses. **Elrond:** B; daily external weights cannot reproduce this timing.

## R04 — VWAP trend pullback and reclaim

**Source/evidence:** [Schwab's VWAP guide](https://www.schwab.com/learn/story/how-to-use-volume-weighted-indicators-trading) supports using position relative to VWAP to interpret intraday buying/selling pressure. It does not establish the following reclaim conditions. Evidence quality is educational; this continuation hypothesis is distinct from R03's extreme-deviation reversal.

**Proposed test:** after 30 minutes, require three consecutive closes above VWAP and current VWAP above its value three bars earlier. Then allow one to three bars below VWAP, with no close below VWAP−s. Buy next bar after a close reclaims VWAP. Stop on a close below the pullback's minimum low minus 0.1 of signal-bar ATR14. Exit at entry+2R, a close below VWAP, twelve bars, or I cutoff; R is entry minus stop. Use S and I. One reclaim trade per session.

**Assets/data:** full 5-minute sessions, traded volume and quotes on liquid equities/ETFs. **Win rate:** UNKNOWN. **Failures:** repeated crossings in flat markets, late entries and spread-sensitive stops. **Elrond:** B; observed indicators alone do not provide full-session execution support.

## R05 — Opening-gap fade after reversal confirmation

**Source/evidence:** [Dahlquist and Bauer's publisher-hosted sample](https://ptgmedia.pearsoncmg.com/images/9780132900430/samplepages/0132900432.pdf) distinguishes opening gaps, continuation and reversal trades. It also explains the hindsight problem in labeling exhaustion gaps. Author exposition supports the setup class, not a claim that every gap fills.

**Proposed test:** regular-session open below yesterday's low, with close-to-open gap between −0.5 and −1.5 of yesterday's ATR. Observe the first three 5-minute bars; require the third close above the first bar's high and below yesterday's close. Buy next bar. Stop on a close below the first-three-bar low minus 0.1 ATR; target yesterday's close, with I cutoff. ATR here is prior daily ATR. Use S and I; one attempt/day, no news or earnings requirement. Short gap-up fades are a grouped variant requiring borrow modeling.

**Assets/data:** equities/ETFs; aligned prior daily and current intraday data, auction prices, corporate-action exclusions. **Win rate:** UNKNOWN. **Failures:** repricing, halts, unfilled gaps and adverse opening spreads. **Elrond:** B; cannot infer the entry sequence from daily OHLC.

## R06 — Opening-gap continuation with held opening range

**Source/evidence:** [Dahlquist and Bauer](https://ptgmedia.pearsoncmg.com/images/9780132900430/samplepages/0132900432.pdf) describe trading in the gap's direction and distinguish observable opening gaps from gaps known to remain unfilled only at the close. Their discussion does not validate this exact opening-range rule. No earnings-surprise condition is included.

**Proposed test:** open above yesterday's high with close-to-open gap 0.5–2 prior daily ATR. All first six 5-minute lows must remain above yesterday's high. After that 30-minute range is fixed, buy next bar following the first close above its high; cancel at 12:00 if absent. Stop on a close below the opening-range low; target entry+2R or I session cutoff. Use S and I. Group downward-gap shorts and range-length sensitivity within this family.

**Assets/data:** liquid equities/ETFs, complete opening bars, splits/dividends and execution costs. **Win rate:** UNKNOWN. **Failures:** trapped late buyers, reversal after the initial range, gap-size selection bias and spreads. **Elrond:** B; a separate next-day daily-gap variant would test a different timing hypothesis.

## R07 — Short-term cross-sectional loser reversal

**Source/evidence:** [Kenneth French's short-term reversal factor construction](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/det_st_rev_factor.html) uses prior-month returns, size groups and low-return-minus-high-return portfolios. This is an explicit, reproducible academic factor definition, not evidence that a long-only sleeve has the same return or risk as the published long/short factor.

**Proposed test:** at each month-end, rank eligible stocks by total return over that completed calendar month. Buy the bottom 20% next open, equal-notional S; hold until next month-end rebalance. Per-position stop: close below entry minus 3 signal-day ATR, then next-open exit; do not refill that sleeve until rebalance. Eligibility: point-in-time US common stocks, month-end price≥$5, trailing twenty-session median dollar volume≥$20m; break ranking ties by ticker. Weekly or five-session formation periods are sensitivity variants, not source rules.

**Assets/data:** broad equities; delistings, membership history, dividends and costs. **Win rate:** UNKNOWN. **Failures:** distressed-stock concentration, microcap artifacts and turnover. **Elrond:** A for long-only weights; reproducing the academic short leg needs borrow/funding support and must be reported separately.

## R08 — Large one-day shock reversal

**Source/evidence:** [Cox and Peterson (1994), Journal of Finance](https://onlinelibrary.wiley.com/doi/10.1111/j.1540-6261.1994.tb04428.x), find that bid-ask bounce and liquidity explain short-term reversals following large price drops; they do not support a simple overreaction interpretation and report poor longer-horizon performance. Peer-reviewed evidence is a warning about tradability, not an endorsement of this proposed rule.

**Proposed test:** completed daily simple return ≤−10%, excluding mechanical split/ex-dividend artifacts; buy next open only if shares traded on the shock day and no halt prevents execution. Exit after three held sessions, on a close at/above the midpoint between the pre-shock close and shock-day close, or below entry−2 prior-day ATR. Freeze ATR before the shock to avoid hindsight. Use S and D, one position per episode; require five subsequent sessions before rearming.

**Assets/data:** liquid common stocks, corporate actions, delisting/halts and spread estimates. **Win rate:** UNKNOWN. **Failures:** informed repricing, bankruptcy, illiquidity and gap-through exits. **Elrond:** A for daily screening; credible validation needs stronger data than clean surviving-symbol OHLC.

## R09 — Bullish engulfing reversal

**Source/evidence:** [Fidelity's chart-pattern teaching, slide 37](https://www.fidelity.com/bin-public/060_www_fidelity_com/documents/learning-center/Idenitfying-Chart-Patterns.pdf) defines real-body engulfing; it does not supply a complete profitable trading algorithm. [Marshall's original thesis](https://mro.massey.ac.nz/items/edc6c1cf-63d9-4c94-a13d-92ffe1a53481), using DJIA stocks over 1992–2002, finds no general candlestick value. Educational pattern evidence is weak; empirical skepticism is material.

**Proposed test:** before the pair, close is below its five-session-earlier close. First candle has C<O; second has C>O, O≤prior C, and C≥prior O, with its real body strictly larger. Buy next open. Stop on a close below the pair's minimum low minus 0.1 ATR; target entry+2R or five sessions. Use S and D; ignore zero-range candles. Bearish engulfing is a grouped mirror, not an independent discovery.

**Assets/data:** daily liquid equities/ETFs, split-consistent opens and closes. **Win rate:** UNKNOWN. **Failures:** arbitrary pattern tolerances, overnight gaps and multiple-testing bias. **Elrond:** A after explicit candle detection; no ready-made strategy inferred from indicator availability.

## R10 — Hammer / pin-bar rejection

**Source/evidence:** [Fidelity's hammer discussion, slides 35–36](https://www.fidelity.com/bin-public/060_www_fidelity_com/documents/learning-center/Idenitfying-Chart-Patterns.pdf) describes the candle family and explicitly notes below-average hammer performance. Pattern names and illustrations are not executable validation. Broader [Marshall evidence](https://mro.massey.ac.nz/items/edc6c1cf-63d9-4c94-a13d-92ffe1a53481) counsels against assuming standalone candle profitability.

**Proposed test:** after a negative five-session return, require body `abs(C−O)` between 10% and 30% of H−L, lower wick at least twice the body, upper wick no more than the body, and close in the upper 30% of the range. Buy next open. Stop on a close below the hammer low−0.1 ATR; target entry+2R or five sessions. Use S and D. Body color is unrestricted; shooting-star and other bearish mirrors remain variants.

**Assets/data:** daily equities/ETFs; stable OHLC and minimum tick handling. **Win rate:** UNKNOWN. **Failures:** visually persuasive noise, intraday sequence ambiguity and narrow-stop slippage. **Elrond:** A with these close-triggered exits; intrabar stop orders would require separately audited fills.

## R11 — Morning-star three-candle reversal

**Source/evidence:** [IG Academy](https://bts.ig.com/uk/ig-academy/the-basics-of-technical-analysis/candlestick-patterns) describes a bearish candle, indecisive star and bullish candle, noting traditional gaps and their absence in continuous markets. It is broker education, without a defined profitable sample. [Marshall's research](https://mro.massey.ac.nz/items/edc6c1cf-63d9-4c94-a13d-92ffe1a53481) supplies general skepticism about candlestick inference, not a result for this precise proposed filter.

**Proposed test:** preceding five-session return negative; candle 1 bearish with body≥0.8 ATR; candle 2 body≤0.3 ATR and body entirely below candle 1's close; candle 3 bullish with close above candle 1's body midpoint. ATR is frozen before candle 1. Buy next open; stop on a close below the three-bar low−0.1 ATR; target entry+2R or five sessions. Use S and D. Gap-relaxed and evening-star variants stay grouped.

**Assets/data:** daily equities/ETFs; distinguish genuine gaps from corporate actions. **Win rate:** UNKNOWN. **Failures:** rare samples, loose star definitions and continuation of the decline. **Elrond:** A; confirmation necessarily delays entry until all three candles finish.

## R12 — Inside-bar compression breakout

**Source/evidence:** [ICE's chart-study documentation](https://download.dataservices.theice.com/products/marketq/help/charts/chart_studies/available_chart_studies/dmi.htm) defines inside days. [Fidelity, slide 44](https://www.fidelity.com/bin-public/060_www_fidelity_com/documents/learning-center/Idenitfying-Chart-Patterns.pdf) explains narrow-range breakout logic. These establish observable compression concepts; neither validates the exact combined rule below. Evidence quality is definitional/educational.

**Proposed test:** today's high<yesterday's high and low>yesterday's low, with today's range the smallest of the last four bars. Fix yesterday as the mother bar. Within the next three sessions, buy next open after a close above the mother-bar high; cancel if a close first falls below its low or the window expires. Stop on a close below the mother-bar low; target entry+2R or ten held sessions. Use S and D. Downward breaks and NR4-only variants remain in the same compression family.

**Assets/data:** daily liquid equities/ETFs, full bars and gap-aware next opens. **Win rate:** UNKNOWN. **Failures:** repeated false breaks, late confirmation and volatility expansion against the position. **Elrond:** A; no assumed fill at the historical breakout price.

## R13 — False breakdown and support reclaim

**Source/evidence:** [Fidelity, slides 12–14](https://www.fidelity.com/bin-public/060_www_fidelity_com/documents/learning-center/Idenitfying-Chart-Patterns.pdf) distinguishes false/failed breakouts and illustrates stop-and-reverse concepts. This supports a recognizable failure mechanism, not positive expectancy or the following levels. It is educational evidence.

**Proposed test:** support is the minimum low of the twenty completed sessions before today. Require today's low below support−0.1 ATR but today's close above support; yesterday's close must be at/above support. Buy next open. Stop on a close below today's low−0.1 ATR; target entry+2R or five sessions. Use S and D; rearm only after a fresh twenty-session support level forms. This is a failed-breakdown reversal, not a standalone channel-breakout system. The failed-upside-breakout short is a grouped variant.

**Assets/data:** daily equities/ETFs; consistent historical lows, opens, corporate actions and costs. **Win rate:** UNKNOWN. **Failures:** arbitrary support windows, downward repricing and repeated attempts against a trend. **Elrond:** A for completed-bar reclaims; any same-bar stop-and-reverse implementation requires B-level execution treatment.

## R14 — Volume-climax downside reversal

**Source/evidence:** [Campbell, Grossman and Wang (1993), author-hosted paper](https://web.mit.edu/wangj/www/pap/CampbellGrossmanWang93.pdf) connects volume to declining return autocorrelation and models liquidity-driven selling with higher subsequent expected returns. It does not validate a particular climax candle. Conversely, [Stickel and Verrecchia (1994)](https://rpc.cfainstitute.org/research/financial-analysts-journal/1994/evidence-that-trading-volume-sustains-stock-price-changes) find strong-volume price changes need not reverse. Peer-reviewed mechanisms conflict with simplistic volume folklore.

**Proposed test:** negative five-day return; today's low below the prior twenty-day minimum; volume≥3×the prior twenty-day median; range≥1.5 prior-day ATR; close in the upper third of today's range. Buy next open; stop on a close below the signal low−0.1 ATR, target entry+2R, or exit after five sessions. Use S and D.

**Assets/data:** equities/ETFs with split-adjusted comparable volumes; no synthetic or tick-volume substitutes. **Win rate:** UNKNOWN. **Failures:** informed selling, volume anomalies, market-wide crashes and selection of rare dramatic examples. **Elrond:** A for daily OHLCV; evidence does not justify labeling every high-volume drop capitulation.

## R15 — OBV bullish divergence with price confirmation

**Source/evidence:** [Fidelity's OBV guide](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/obv) describes positive divergence when price makes lower troughs without corresponding OBV lows. It supports the indicator interpretation, not an independently validated entry/exit model. Divergence must be defined without future pivot information.

**Proposed test:** initialize OBV at zero; add volume on higher closes, subtract on lower closes, unchanged on equal closes. Identify two local closing-price lows, each lower than the two closes on either side and separated by 5–20 sessions. Only recognize a pivot after its two right-side bars complete. Require the second price low lower but its OBV higher. Within five subsequent bars, buy after a close above the highest close between pivots. Stop below second-pivot low−0.1 ATR; target entry+2R or ten sessions. Use S and D.

**Assets/data:** daily liquid equities with accurate volumes. **Win rate:** UNKNOWN. **Failures:** delayed confirmation, structural volume changes and cherry-picked pivots. **Elrond:** A; offline logic must enforce pivot confirmation dates and cancel expired setups.

## R16 — Stochastic oversold bullish cross

**Source/evidence:** [Fidelity's fast stochastic guide](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/fast-stochastic) describes %K/%D crossovers in oversold regions and conventional 20/80 boundaries. Broker education supports an operational signal family, without a population-defined profitable result. Oversold describes location within a range, not undervaluation.

**Proposed test:** fast `%K=100×(C−lowest low14)/(highest high14−lowest low14)` and `%D=SMA3(%K)`. Require prior %K≤prior %D, current %K>current %D, and both current lines below 20; buy next open. Skip zero denominators. Exit on a close producing %K≥80, below entry−2 ATR, or after ten sessions. Use S and D. A 50-level exit, slower smoothing, and overbought bearish crosses are parameter/direction variants, not new candidates.

**Assets/data:** daily liquid ETFs/equities, complete highs/lows, warm-up and trading costs. **Win rate:** UNKNOWN. **Failures:** oscillator pinning during trends, repeated stopouts and tiny range-bound gains after spreads. **Elrond:** A after offline rule generation; shared oscillator inputs do not establish implementation of this trading rule.

## Evidence limits and research ordering

All cited pages were opened online during this research. The primary-source NBER Lehmann page/PDF and the publisher's 2006 candlestick article were access-blocked; no detailed claims here depend on their inaccessible full text. R07 instead uses the accessible French construction, and candlestick skepticism cites the original author's university thesis record. No paid rulebook, social-media backtest or unsourced win-rate claim was used.

Practical first comparisons are R01/R02/R07/R16 on the daily scaffold, with R08/R14 as explicit stress/liquidity hypotheses. Candles R09–R13 should face simple price-change and volatility-matched controls before more parameters are added. Intraday R03–R06 remain data/execution projects. This ordering reflects testability and evidence discipline, not a prediction of profitability.
