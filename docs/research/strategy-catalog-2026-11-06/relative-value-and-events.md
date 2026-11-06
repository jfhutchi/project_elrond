# Relative value, carry, and event candidates — research only

Research date: 2026-11-06. These are 16 distinct hypotheses, not implemented strategies, deployment approvals, or promises of a 60–70% win rate. Source pages, abstracts, and available papers were browsed; access limitations are identified below. Historical evidence establishes a research rationale, not present profitability. **Reported trade win rate: UNKNOWN for every candidate below.** Annual returns, Sharpe ratios, positive portfolio months, and event-average returns are not trade win rates.

**Operational conventions added for this catalog:** `U` means one normalized unit of gross research notional, with the sum of absolute leg weights equal to one; cash is the remainder for long-only portfolios. It is a comparison convention, not an account allocation, recommended leverage, collateral specification, or new risk setting. Paired and short exposures are **UNAUTHORIZED** and require extension. Unless explicitly described as an author's rule, every numerical threshold, holding period, stop, universe filter, and fill convention below is a proposed operational definition. Signal inputs must be available before the specified entry; daily signals execute at the next available session open. Stops execute at the first executable price after triggering and do not guarantee the stated loss. No candidate uses martingale sizing or averaging down.

**Elrond classification:** **A** = plausible research use of the existing daily long-only `external_weights` / `EXTERNAL_SIGNAL` next-session scaffold, with a new signal and required data still to be supplied. **B** = event-data, intraday timing, auction, or execution-model extension required. **C** = new asset-class, short/multi-leg, margin/collateral, or settlement engine required. Current paper execution is long-only equity eligibility; its learning calendar is daily XNYS. A crypto data adapter and offline capability do not establish integrated crypto paper execution. No classification claims live, futures, FX, or derivatives access.

## X01 — Distance-selected equity pairs

**Source/evidence:** [Gatev, Goetzmann and Rouwenhorst, *Pairs Trading* (NBER working paper; subsequently RFS)](https://www.nber.org/papers/w7032) tests minimum-distance normalized-price pairs with six-month trading periods using 1962–1997 daily data. The historical evidence is substantial but old; the authors explicitly identify microstructure as a possible contributor. It supports paired relative convergence, not a guaranteed neutral payoff.

**Proposed rules:** For liquid same-sector U.S. equities, use 252 completed sessions of dividend-adjusted prices, normalize each series to one, and select disjoint pairs minimizing summed squared distance. Freeze parameters for 126 sessions. Enter next open when the normalized spread exceeds two formation-period standard deviations; buy the cheaper leg and short the richer. Exit on zero crossing, a four-standard-deviation adverse spread, or 20 sessions. Size each leg at U/2; no additions to losing trades.

**Needs/risks/support:** Point-in-time universes, delistings, dividends owed on shorts, borrow availability and fees, synchronized executable prices, and joint fills are essential. Structural breaks, takeover gaps, crowding, and forced buy-ins defeat convergence. Trade win rate **UNKNOWN**. **C; shorts and paired execution UNAUTHORIZED.**

## X02 — Cointegration-filtered equilibrium pairs

**Source/evidence:** [Brunetti and De Luca, *Pre-selection in cointegration-based pairs trading* (2023)](https://link.springer.com/article/10.1007/s10260-023-00702-4) is an open, peer-reviewed comparison showing that pair pre-selection changes returns and risk even after commissions, cut rules, and alternative cointegration tests. This is a distinct equilibrium hypothesis from X01's distance matching, not proof that every correlated pair converges.

**Proposed rules:** Monthly, fit log-price relationships to 504 completed daily observations of liquid same-industry equities. Keep only positive hedge coefficients and residual stationarity passing a predeclared multiple-testing adjustment; use a 1% adjusted rejection threshold. Freeze each selected hedge until the next monthly review. Enter at residual z-score ±2, trade toward equilibrium, and exit at |z|≤0.25, adverse |z|≥3.5, or 30 sessions. Scale the fitted long/short hedge to gross U.

**Needs/risks/support:** Use point-in-time membership, split-consistent histories, borrow costs, financing, and two-leg fills. Regression orientation, selection bias, unstable hedge ratios, and false stationarity are material risks; a statistical rejection is no insurance against permanent repricing. Trade win rate **UNKNOWN**. **C; short/multi-leg exposure UNAUTHORIZED.**

## X03 — Sector-ETF or PCA residual mean reversion

**Source/evidence:** [Avellaneda and Lee, *Statistical Arbitrage in the U.S. Equities Market*](https://math.nyu.edu/~avellane/AvellanedaLeeStatArb071008.pdf) models stock-return residuals after sector-ETF or PCA factor removal. Its 1997–2007 historical tests include transaction costs and show deterioration after 2002, plus vulnerability during the August 2007 unwind. The paper uses 1.25-standardized-score entry thresholds; the following is a simplified research specification, not a replication.

**Proposed rules:** Fit a 60-session stock/sector-ETF regression each day using completed observations; model accumulated residuals as mean reverting. Enter next open when the standardized residual is below −1.25, long stock and short its positive-beta ETF hedge; reverse above +1.25. Exit at |score|≤0.5, adverse |score|≥3, or ten sessions. Normalize the combined hedge to U. Treat PCA factor removal as a prespecified comparator within this family, not another catalog entry.

**Needs/risks/support:** Requires point-in-time sector mappings, ETF history, stable factor estimation, borrow and financing, and multi-leg fills. Residual earnings jumps, crowded exits, changing betas, and underestimated impact matter. Trade win rate **UNKNOWN**. **C; hedged shorts UNAUTHORIZED.**

## X04 — Cross-asset value sleeves with periodic rebalancing

**Source/evidence:** [Asness, Moskowitz and Pedersen, *Value and Momentum Everywhere* (2013)](https://pages.stern.nyu.edu/~lpederse/papers/ValMomEverywhere.pdf) reports value premia and common risks across markets. This candidate uses only value. The paper supports within-market relative value, not ranking unrelated assets using one raw ratio.

**Proposed rules:** At each month-end rank within separate country-equity, government-bond, currency, and commodity sleeves. Use equity book-to-market, real bond yield, real-exchange-rate cheapness, and negative five-year real commodity return respectively, with all publications lagged to availability. Hold the cheapest third and short the dearest third until next rebalance. Allocate U equally across sleeves and normalize each sleeve's gross legs. Stop a sleeve at the next review if required observations are missing or its valuation definition becomes invalid; impose no price stop in this baseline.

**Needs/risks/support:** Requires comparable accounting, inflation and yield vintages, contract histories, cross-market calendars, FX translation, roll/borrow/financing. Cheapness can persist for years; correlated funding stress and structural value traps remain. Trade win rate **UNKNOWN**. **C; multiasset shorts UNAUTHORIZED.**

## X05 — FX forward carry basket

**Source/evidence:** [Brunnermeier, Nagel and Pedersen, *Carry Trades and Currency Crashes*](https://www.nber.org/papers/w14473) documents negative skew in high-interest versus low-interest currencies and links unwinds to reduced risk appetite and funding liquidity. It establishes crash exposure, not safe income.

**Proposed rules:** On each month-end, rank a fixed developed-currency universe by executable one-month forward-implied carry versus USD. At the next liquid fixing, buy the highest-carry third and sell the lowest-carry third through matched-tenor forwards. Equal-weight currencies within each side and normalize combined gross exposure to U. Settle or close after one month and re-rank; do not automatically compound losses. Baseline stop is contract expiry, with early closure if a quote/settlement source becomes invalid; no price stop is assumed.

**Needs/risks/support:** Requires spot/forward bid-ask histories, exact quote direction, holidays, settlement conventions, collateral and financing cashflows, counterparty limits, and stress marks. Funding-currency rallies and correlated devaluations can erase many small gains; stops cannot prevent gaps. Trade win rate **UNKNOWN**. **C; FX, short exposure, and collateral infrastructure absent/UNAUTHORIZED.**

## X06 — FX real-valuation reversion

**Source/evidence:** [Menkhoff, Sarno, Schmeling and Schrimpf, *Currency Value* (accepted RFS manuscript)](https://openaccess.city.ac.uk/14851/13/FXVALUE_Rev3_cepr.pdf) finds cross-sectional predictability from real exchange rates and stronger results after conditioning on macro fundamentals. The paper distinguishes currency value from carry. Its macro-adjusted result should not be attributed to a simple purchasing-power-parity screen.

**Proposed rules:** Monthly compute q=log(CPI_US)−log(CPI_foreign)−log(S), with S in USD per foreign unit and publication-lagged CPI; higher q indicates foreign-currency cheapness. Standardize q against its trailing ten-year mean and deviation. Buy the cheapest third and sell the dearest third using one-month forwards; gross U, equal weights within each side. Rebalance monthly. Exit when a currency leaves its extreme group or crosses its valuation mean; no price stop, and cease entries when CPI is stale by over two scheduled releases.

**Needs/risks/support:** Requires CPI vintages, consistent base periods, forward costs, settlement and collateral. Productivity shifts and persistent PPP deviations can defeat convergence; cheap currencies can keep falling. Trade win rate **UNKNOWN**. **C; FX/multi-leg shorts UNAUTHORIZED.**

## X07 — Commodity futures curve carry

**Source/evidence:** [Koijen, Moskowitz, Pedersen and Vrugt, *Carry* (NBER; subsequently JFE)](https://www.nber.org/papers/w19325) finds carry predictability across asset classes and common poor performance during global recessions. It motivates term-structure research; backwardation is not riskless yield.

**Proposed rules:** Monthly compute annualized curve carry as log(near/deferred futures price) divided by maturity separation, using the first two liquid, positive-priced contracts whose delivery windows are outside the next 20 sessions. Buy the highest-carry third and short the lowest-carry third across a fixed commodity universe. Allocate equal gross notional per market and normalize to U. Re-rank monthly; close or roll at the earlier of ten sessions before first notice or last trading day. This mandatory expiry exit is the baseline stop; no price stop is assumed.

**Needs/risks/support:** Requires individual-contract histories, multipliers, first-notice/expiry calendars, daily variation margin, curve quotes, roll spreads and limit rules. Storage shocks, seasonality, delivery squeezes, negative prices and limit moves undermine simple carry estimates. Trade win rate **UNKNOWN**. **C; futures/short/margin execution UNAUTHORIZED.**

## X08 — Dated crypto spot–futures basis convergence

**Source/evidence:** [BIS Working Paper 1087, *Crypto carry*](https://www.bis.org/publications/working-paper-1087-crypto-carry) studies Bitcoin and Ether cash-and-carry: long spot and short dated futures. It links large bases to crypto market structure and boom/bust dynamics. This institutional research does not establish collectible net yield.

**Proposed rules:** Daily inspect liquid 30–90-day contracts. Enter matched spot-long/future-short exposure only when executable basis receipts exceed three times estimated two-sided fees, financing, custody and settlement costs. Normalize the two legs to gross U. Hold until settlement or until executable remaining basis falls below remaining closeout costs; close early if settlement-index or collateral data becomes invalid. No price stop is assumed because net basis and collateral govern the trade.

**Needs/risks/support:** Requires synchronized venue quotes, actual contract and settlement specifications, collateral cashflows, custody, venue and withdrawal histories. Basis widening, liquidation before convergence, exchange failure and settlement mismatch can dominate apparent yield. Trade win rate **UNKNOWN**. **C; crypto paper derivatives and paired shorts are not integrated and are UNAUTHORIZED.**

## X09 — Spot–perpetual funding carry

**Source/evidence:** [He, Manela, Ross and von Wachter, *Fundamentals of Perpetual Futures*](https://arxiv.org/abs/2212.06888) develops perpetual pricing and trading-cost bounds, with empirical crypto deviations. The browsed author preprint was revised September 2026. Perpetuals lack X08's dated convergence; recent positive funding does not lock future returns.

**Proposed rules:** For liquid BTC or ETH spot/perpetual pairs, estimate the next day's funding from the preceding seven complete days. Enter spot-long/perpetual-short only when forecast funding exceeds estimated round-trip trading and financing costs. Scale matched quantities to gross U. Evaluate each funding settlement; exit when forecast net funding becomes nonpositive, a venue's mark/index feed fails, or seven calendar days elapse. No spot-price stop; an executable net-basis loss reaching two times the initial projected funding receipts triggers paired exit.

**Needs/risks/support:** Requires exact funding timestamps/formulas, payment history, mark/index prices, contract type, collateral and liquidation modeling, and 24/7 processing. Funding flips, basis dislocation, depegs and platform failure create negatively skewed outcomes. Trade win rate **UNKNOWN**. **C; perpetual/margin/short execution UNAUTHORIZED.**

## X10 — Post-earnings-announcement drift (PEAD)

**Source/evidence:** [Bernard and Thomas, *Post-Earnings-Announcement Drift: Delayed Price Response or Risk Premium?* (1989)](https://www.jstor.org/stable/2491062) is the classic peer-reviewed earnings-surprise study. Bibliographic identity and indexed reproduced text were verified; JSTOR full text was not machine-readable. [A primary follow-up hosted by NYU](https://archive.nyu.edu/bitstream/2451/27222/2/wpa94022.pdf) also identifies delayed adjustment. Historical evidence motivates multiweek drift, not the exact rules below.

**Proposed rules:** For liquid U.S. equities, calculate standardized unexpected earnings from reported EPS minus same-quarter-prior-year EPS, divided by the standard deviation of the preceding eight such differences. Rank against eligible announcements from the preceding 90 days. Buy positive top-decile surprises at the first open after one complete post-release session. Hold 60 sessions or until the next earnings release. Stop after a close two pre-entry ATR(20) below entry, executing next open; equal-weight active positions within U.

**Needs/risks/support:** Requires point-in-time EPS, release timestamps, restatement handling and delistings. Guidance reversals, stale consensus proxies, crowding and earnings gaps remain. Trade win rate **UNKNOWN**. **B** for event data; daily long-only weights can use **A** once data exists.

## X11 — Earnings-conditioned gap continuation, first session only

**Source/evidence:** [Patell and Wolfson, *The Intraday Speed of Adjustment of Stock Prices to Earnings and Dividend Announcements* (Stanford)](https://www.gsb.stanford.edu/faculty-research/publications/intraday-speed-adjustment-stock-prices-earnings-dividend-announcements) reports simple-rule gains dissipating within minutes. [Gerlach and Lee's primary working-paper abstract](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1570343) finds overnight earnings information reflected by the open. Indexed abstracts were readable; direct retrieval returned access errors. These partly negative sources make this proposal **exploratory**, not an established opening edge.

**Proposed rules:** Require a verified after-close/before-open earnings release, positive EPS surprise, and next-session opening gap exceeding one prior ATR(20). Enter a long at 09:35 only if the executable midpoint remains above the official open. Exit at 15:55 or when executable bid falls below the official open. Equal-weight qualifying names within U; no overnight holding.

**Needs/risks/support:** Liquid U.S. equities; minute quotes, release/consensus vintages, auctions, halts and spreads. The earnings condition distinguishes this from generic gap continuation and X10's multiweek drift. Opening overreaction, adverse selection and a single adverse halt can dominate. Trade win rate **UNKNOWN**. **B; intraday/event extension required.**

## X12 — Turn-of-month equity exposure

**Source/evidence:** [McConnell and Xu, *Equity Returns at the Turn of the Month*](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1135217) reports U.S. return concentration near month boundaries. Its indexed author abstract was accessible; direct retrieval was blocked. [Kayacetin's 2026 primary study](https://www.sciencedirect.com/science/article/pii/S1042443126000259) tests 30 countries over 1994–2023 and identifies the conventional last-session/first-three-session window. These are portfolio averages, not win rates.

**Proposed rules:** Hold one U of a liquid broad U.S. equity ETF from the close immediately before the month's last trading session through the close of the next month's third trading session; otherwise remain in cash. Fix the XNYS calendar in advance. Exit at that scheduled close; no additional price stop in the baseline. Model the actual closing auction and order cutoff, since the rule is known beforehand.

**Needs/risks/support:** Requires historical calendars, holiday exceptions, auction prices, distributions, spread/fees and cash interest. Few independent monthly observations, overlapping macro events, flow changes and lost market exposure matter. Trade win rate **UNKNOWN**. Exact close-to-close rule is **B**; a next-open timing variant can use **A**, with separately measured results.

## X13 — Pre-holiday equity session premium

**Source/evidence:** [Ariel, *High Stock Returns before Holidays* (1990)](https://onlinelibrary.wiley.com/doi/10.1111/j.1540-6261.1990.tb03731.x) documents historical pre-holiday returns. [Ko and Yang's primary replication](https://cfr.ivo-welch.info/published/papers/ko2021pre.pdf) confirms the old finding but finds the 1983–2019 effect concentrated in small firms; large-firm differences became insignificant especially after 1990. Liquid large-cap implementation evidence is weak.

**Proposed rules:** Buy one U of a liquid broad equity ETF at the close immediately preceding the last exchange session before an official full-day XNYS holiday; sell at the close of that pre-holiday session. Treat early closes explicitly. Scheduled exit is the stop; no additional price stop or post-holiday holding. A separately reported small-cap ETF comparator tests transferability without substituting illiquid historical microcaps.

**Needs/risks/support:** Requires point-in-time holiday calendars, genuine closing auction executions, spreads, dividends and cash returns. Small samples, calendar overlap, reduced liquidity and anomaly decay are central; evidence about small stocks does not directly establish an ETF effect. Trade win rate **UNKNOWN**. Exact close timing is **B**; a daily next-open adaptation is **A** and must not inherit the original results.

## X14 — Overnight versus intraday allocation by return persistence

**Source/evidence:** [Lou, Polk and Skouras, *A Tug of War* (2019)](https://personal.lse.ac.uk/polk/research/TugOfWar.pdf) documents persistence within overnight and intraday stock returns, alongside cross-component reversal. Its 1993–2013 evidence also decomposes established strategies; it does not endorse universal overnight ownership.

**Proposed rules:** At month-end, rank liquid U.S. equities by prior 21-session average close-to-open return. For the next month, hold an equal-weight top-decile long basket only overnight: enter in the closing auction and exit in the next opening auction. Normalize to U and remain cash intraday. Re-rank monthly; next-open liquidation is the time stop, with no assumed overnight price stop. Compare the same basket held intraday and continuously as evaluation arms of one hypothesis.

**Needs/risks/support:** Requires split/dividend-consistent opens and closes, real auction prices, opening spreads, timestamp alignment and turnover costs. Overnight news gaps, crowded auctions, stale opens and frequent round trips can remove the paper effect. Trade win rate **UNKNOWN**. Long-only transfer is an unproven adaptation; **B** for overnight/intraday execution.

## X15 — Day-of-week exposure as one seasonality family

**Source/evidence:** [French, *Stock Returns and the Weekend Effect* (1980)](https://www.sciencedirect.com/science/article/pii/0304405X80900215) finds negative average Monday returns and positive other-weekday averages in the 1953–1977 S&P sample. This historical documentation does not establish survival under current trading and settlement conventions. Weekdays constitute one family, not five strategies.

**Proposed rules:** Test one preregistered long-only exclusion rule on a liquid broad equity ETF: own U from Monday's close to Friday's close, hold cash from Friday close through Monday close, and do not optimize weekday choices after observing results. When Monday is closed, remain cash through the first subsequent session's close. Scheduled transitions are the only stops; no price stop in the baseline.

**Needs/risks/support:** Requires exchange-local calendars, holiday treatment, distributions, cash interest, and executable closing auctions. A weekday dummy must not accidentally absorb turn-of-month or holiday effects; transaction costs and foregone weekend premia matter. Trade win rate **UNKNOWN**. Exact published close-to-close timing is **B**; an independently assessed next-open variant can use **A**.

## X16 — Scheduled pre-FOMC equity drift

**Source/evidence:** [Lucca and Moench, NY Fed Staff Report 512](https://www.newyorkfed.org/medialibrary/media/research/staff_reports/sr512.html) documents elevated equity returns before scheduled FOMC decisions. [Kurov, Wolfe and Gilbert, *The Disappearing Pre-FOMC Announcement Drift*](https://doi.org/10.1016/j.frl.2020.101781) extends the sample through 2019 and finds the drift essentially disappeared after 2015. This is a low-priority persistence falsification, not presumed contemporary alpha.

**Proposed rules:** For each previously scheduled decision, buy U of a liquid broad U.S. equity ETF 24 hours before the publicly scheduled release timestamp and sell five minutes before it. Skip unscheduled meetings and events whose timing was not known before entry. If the preceding-day timestamp falls outside a regular session, skip rather than invent an overnight fill. Use the scheduled exit as the stop; no guaranteed price stop across the overnight interval.

**Needs/risks/support:** Requires historical announcement schedules and revisions, minute quotes, time zones, auction/early-close handling, and spreads. Timing revisions, pre-release shocks, scarce independent events and publication decay dominate. Trade win rate **UNKNOWN**. **B; the daily XNYS scaffold cannot reproduce the event window.**

## Evidence and implementation boundary

All stops, sizing conventions and timing adaptations above are research definitions, not requests to enable them. Source-backed means a verified research rationale or explicit negative evidence; it does not mean that each proposed operational variant was studied by the cited authors. In particular X11, X13 and X16 should survive a demanding falsification gate before further engineering. X08 and X09 must remain separate because dated convergence and perpetual funding are different economic mechanisms. High observed carry win rates, if later found, must be assessed alongside net expectancy, rare losses, collateral demands and complete drawdowns. No strategy here justifies a target hit rate without a fully specified trade denominator and independent, cost-aware evidence.
