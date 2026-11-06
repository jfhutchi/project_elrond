# Strategy research catalog — November 6, 2026

This catalog records **48 distinct research candidates**: T01–T16, R01–R16 and X01–X16. No candidate was implemented, backtested or activated by this documentation work. The current implementation priority is **crypto spot**; direct international equities, FX and options are **DEFERRED**. Retained research on those markets does not put them into the current delivery scope.

Read the [evaluation plan](evaluation-plan.md) before implementing rules or interpreting results, and the [market and sleep roadmap](market-and-sleep-roadmap.md) for market boundaries and dated operating scenarios. The catalog's stock/ETF research definitions do not automatically transfer to crypto: session boundaries, symbol eligibility, costs and execution require separate specifications.

## Evidence and fit

Sources range from peer-reviewed research and author papers to broker education and indicator documentation. The family documents record the sources actually inspected, access limitations, negative findings, and differences between published rules and our **proposed** tests. An accessible abstract is not a full-paper review; indicator mechanics are not evidence of profitable trading. This index synthesizes that research without claiming another independent source review.

**Applicable trade win rate is UNKNOWN for all 48 proposed candidates.** Published returns, positive months and another rule's historical hit rate do not establish a 60–70% net trade win rate here. Prioritize out-of-sample net expectancy, costs, drawdown and selection-aware uncertainty. Parameter variants and bearish mirrors remain within their parent ID; related candidates are not independent evidence.

| Fit | Meaning |
|---|---|
| A | Reuse the daily long-only `EXTERNAL_SIGNAL` / `external_weights` accounting scaffold after supplying and validating offline rules and data. |
| B | Additional indicator/state, event-data, intraday, auction or execution handling is needed. |
| C | New asset, short/multi-leg, collateral or settlement capability is needed. |

Fit describes engineering reuse, not implementation, profitability or activation authority. Labels below refer to the documented stock/ETF adaptation; full original replications can require more support.

## All 48 candidates

### [Trend, momentum and breakout definitions](trend-and-breakout.md)

| ID | Candidate | Fit |
|---|---|---|
| T01 | Time-series momentum | A; original futures C |
| T02 | Cross-sectional 12-minus-1 momentum | A |
| T03 | Dual relative-plus-absolute momentum | A |
| T04 | Monthly moving-average timing | A |
| T05 | Donchian channel breakout | A; full Turtle B+C |
| T06 | Fast/slow moving-average crossover | A |
| T07 | MACD signal-line crossover | A+B |
| T08 | ADX-gated directional movement | A+B |
| T09 | Ichimoku cloud trend | A+B |
| T10 | Parabolic SAR | A+B; intrabar reversal B+C |
| T11 | Nearness-to-52-week-high ranking | A |
| T12 | Industry momentum rotation | A; historical classifications B |
| T13 | Opening-range breakout | B |
| T14 | Early-return / late-session momentum | B |
| T15 | Rising-EMA pullback and recovery | A+B |
| T16 | Bollinger squeeze breakout | A+B |

### [Reversion and price-action definitions](reversion-and-price-action.md)

| ID | Candidate | Fit |
|---|---|---|
| R01 | RSI(2) pullback in an uptrend | A |
| R02 | Bollinger lower-band reentry | A |
| R03 | Intraday VWAP deviation reversal | B |
| R04 | VWAP trend pullback and reclaim | B |
| R05 | Opening-gap fade | B |
| R06 | Opening-gap continuation | B |
| R07 | Short-term cross-sectional loser reversal | A; original short leg C |
| R08 | Large one-day shock reversal | A; halt/liquidity data required |
| R09 | Bullish engulfing reversal | A |
| R10 | Hammer / pin-bar rejection | A |
| R11 | Morning-star reversal | A |
| R12 | Inside-bar compression breakout | A |
| R13 | False breakdown and support reclaim | A |
| R14 | Volume-climax downside reversal | A |
| R15 | OBV bullish divergence | A |
| R16 | Stochastic oversold bullish cross | A |

### [Relative-value, carry and event definitions](relative-value-and-events.md)

| ID | Candidate | Fit |
|---|---|---|
| X01 | Distance-selected equity pairs | C |
| X02 | Cointegration-filtered pairs | C |
| X03 | Sector/PCA residual mean reversion | C |
| X04 | Cross-asset value sleeves | C |
| X05 | FX forward carry | C; DEFERRED |
| X06 | FX real-valuation reversion | C; DEFERRED |
| X07 | Commodity futures curve carry | C |
| X08 | Dated crypto spot–futures basis | C; derivatives remain deferred |
| X09 | Spot–perpetual funding carry | C; derivatives remain deferred |
| X10 | Post-earnings-announcement drift | A+B |
| X11 | Earnings-conditioned opening gap | B |
| X12 | Turn-of-month equity exposure | B; distinct next-open variant A |
| X13 | Pre-holiday equity premium | B; distinct next-open variant A |
| X14 | Overnight/intraday return persistence | B |
| X15 | Day-of-week exposure | B; distinct next-open variant A |
| X16 | Scheduled pre-FOMC drift | B |

## Proposed first research batch: 12 candidates

This is a proposed **offline evaluation order**, chosen for testability, evidence diversity and limited new infrastructure. It does not postpone crypto integration, select deployed strategies or rank expected profitability. Run feasibility screening for all 48 IDs first; an unavailable dataset produces a recorded deferral, not a substitute rule presented as the same result.

| Order | ID | Fit | Reason to include; principal qualification |
|---|---|---|---|
| 1 | T01 | A | Simple own-return trend baseline; ETF transfer differs from futures evidence. |
| 2 | T04 | A | Transparent monthly timing comparator; next-open execution changes source results. |
| 3 | T05 | A | Price-extrema breakout comparator; simplified rules are not a Turtle replication. |
| 4 | T02 | A | Cross-sectional momentum benchmark; point-in-time universes and delistings are required. |
| 5 | T11 | A | Annual-high ranking adds an anchoring hypothesis; measure overlap with momentum. |
| 6 | T12 | A / B | Sector-ETF rotation checks industry structure; historical stock classifications need B. |
| 7 | R01 | A | Short-horizon pullback contrast; commercial historical claims do not validate this rule. |
| 8 | R02 | A | Explicit band-reentry hypothesis; test trend-day losses and costs. |
| 9 | R07 | A | Academic reversal construction motivates a long-only comparison; short-factor results do not transfer. |
| 10 | R16 | A | Simple oscillator comparator; educational mechanics provide no demonstrated edge. |
| 11 | X10 | A+B | Event-based drift broadens the mechanism set; block on point-in-time earnings data. |
| 12 | X12 | B | Calendar exposure comparator; exact auctions need B, and any A timing adaptation is a separate test. |

No C candidate enters this initial batch. Crypto spot work does not authorize the short derivatives legs in X08/X09. Transfer a surviving rule to another asset only as a new, logged trial with its own data, costs and calendar contract. Preserve negative results and follow the [evaluation plan](evaluation-plan.md) for frozen rules, chronological splits, trial accounting, cost stress and promotion evidence.
