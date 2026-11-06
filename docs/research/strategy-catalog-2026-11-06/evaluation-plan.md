# Elrond strategy evaluation plan

Research date: November 6, 2026, America/New_York. This is a proposed research protocol, not an implementation, backtest result, strategy activation, or change to trading authority.

## What success means

The requested 60–70% win rate means the fraction of **completed trades with positive net P&L**, not the fraction of profitable calendar days. Report both separately. Breakeven trades remain in the denominator and are reported separately; open trades are marked and disclosed, never silently excluded from portfolio risk. A paper trade with unknown actual fees has unknown actual net success. A backtest may instead report **modeled-net win rate**, with its cost model named.

Win rate is a secondary objective. Rank candidates by positive out-of-sample net expectancy, robustness, capital exposure, drawdown and tail losses. A strategy with fewer winning trades can still be preferable. With average gross win W, average gross loss L and average round-trip cost c, expectancy is pW − (1−p)L − c. For example, 70% wins of 1 unit and 30% losses of 3 units lose 0.2 units per trade before costs; 40% wins of 2 units and 60% losses of 1 unit earn 0.2 before costs. These are arithmetic illustrations, not measured strategy results.

A displayed 65% win rate also needs uncertainty. Illustrative 95% Wilson intervals, calculated for independent binary outcomes with z=1.96, are 55.25–73.64% for 65/100, 60.20–69.51% for 260/400, and 61.99–67.89% for 650/1,000. These are **not minimum-sample guarantees**: clustered trades and repeated selection make uncertainty larger. Use day/week block resampling and report independent time coverage as well as trade count. [NIST proportion intervals](https://itl.nist.gov/div898/handbook/prc/section2/prc241.htm).

## What Elrond actually offers

Source inspection is anchored to commit `eed788b638e8554648325af1922defb79319da14` before this documentation change.

| Existing component | Reuse and limit |
|---|---|
| `src/quantbot/strategy/adaptive_momentum.py` | Existing 12-minus-1 momentum ranking, moving-average filters, Donchian rules and ATR protection. A useful comparator, not proof of predictive value. |
| `src/quantbot/backtest/engine.py`, `benchmarks.py` | Long-only, session-based simulation with next-open fills and modeled commission/slippage. `EXTERNAL_SIGNAL` accepts dated target weights through the existing accounting path. New daily rule generation still needs implementation and characterization. |
| Current `paper_core` execution/learning | Separate five-minute exploration runtime with a 30-minute holding policy and 1% stop. Those horizons must not be silently imposed on monthly or overnight research ideas. |
| Current learner | `COLLECT` is nonpredictive; the supplied runtime review records three attempted candidates rejected by validation, not an absence of training attempts. The current gross-return target can admit otherwise valid fee-pending outcomes. Missing fees are not its current training blocker. |
| `src/quantbot/market_data/crypto.py` | Crypto data support exists. Broker-paper eligibility, symbol handling and learning calendars still need integration for crypto. |
| Accounting | Real aggregate fees are retained. Exact fill-level fee allocation and complete historical cash coverage remain unverified. A matching retained-cash comparison does not turn gross labels into verified net labels. |

The catalog's **A** label means a reusable daily long-only scaffold; **B** means new rule/data/execution handling; **C** means a new asset, short or multi-leg accounting capability. None means “all candidate rules already implemented” or “ready to activate.” Some candidates need more than one label.

## Common reproducibility contract

Every catalog entry inherits these conventions unless it explicitly replaces one. All operational numbers introduced by this research are **proposed hypotheses**, not approved runtime risk settings.

1. Freeze a strategy ID/version, universe-as-of rule, data/feed, session timezone, indicator formulas and warmup, entry and exit rules, stop and cancellation rules, maximum holding period, cost model, benchmark, size convention, and allowed parameter trials before inspecting performance. Store source URLs and distinguish author rules from our test specification.
2. A close-based signal uses only completed bars. Enter at the next available executable bar/open; a same-close fill requires separately modeled auction eligibility and a timely submission rule. Intraday entries use the next bar/quote after the signal is known. Never trade at an earlier high, low or close because it appears in the completed bar.
3. Stop-first, target-first and signal-exit ordering must be defined. If both stop and target are touched within an OHLC bar and finer data cannot resolve order, use the adverse ordering and report the ambiguity rate. Gap-through stops execute at the next executable adverse price, not the stop level. A time exit takes the next executable observation, not a retrospectively selected close.
4. Use Wilder ATR where an entry says ATR without another definition. Proposed ATR lookbacks and stop distances are research parameters; they do not alter deployed controls. A “stop” bounds intended exit behavior, not realized gap loss.
5. Start with one-unit or equal-notional-normalized economic comparisons, constrained by instrument lot sizes. Compare portfolio results at a common unlevered exposure convention. A later risk-normalized pass may use ex-ante volatility and stop distance within a separately approved risk envelope. Do not optimize size to reach the win-rate target. Research requiring shorts or leverage stays offline and explicitly unsupported by current execution authority.
6. For US regular-session intraday rules, use exchange calendars, 09:30–16:00 America/New_York, holidays and early closes. Preload available history before the open. An opening-range strategy intentionally waits for its range; this is not a reason to delay unrelated market-open strategies. Overnight and 24/7 assets need their own calendars.
7. Use point-in-time listings and memberships, delisted securities, corporate actions, splits and dividend treatment. Signal adjustments and executable prices must be consistent without using future adjustments or double-counting dividends. Never choose today's surviving stocks as the historical universe.
8. Keep every rejection reason: unavailable data, no signal, costs, liquidity, exposure, missing eligibility, stale information, or operational failure. A quiet day is a valid observation. No strategy is rewarded for forced trades or hiding losses.

## Test all candidates without an uncontrolled search

**Pass 0 — feasibility for every catalog ID.** Verify public rule reproducibility, legal data availability, tradability, historical universe and clock support. Give each candidate a recorded result: READY_FOR_RULE_IMPLEMENTATION, DATA_REQUIRED, EXECUTION_EXTENSION_REQUIRED, or REJECTED_WITH_REASON. This covers the whole catalog without pretending unavailable futures or short positions can run in today's engine.

**Pass 1 — the prioritized batch in the index.** Implement only those offline rule definitions when separately authorized. Use one frozen default and at most two predeclared sensitivity settings per candidate initially. This is a proposed compute-control budget, not a claim that 36 trials are statistically safe. Log every run, including abandoned variants, asset universes, date-window changes and cost assumptions. Do not run large Cartesian grids.

**Pass 2 — the remaining candidates by family.** Reuse immutable market-data partitions and shared indicators. Admit the next batch only after reviewing the previous results and data gaps; retain negative results. Cross-asset applications and parameter settings are additional trials of the same strategy, not newly counted “strategies.” Pair selection, formation windows and alternative event filters also count as research choices.

**Pass 3 — independent replication and forward observation.** Reproduce finalists from a clean manifest, then use a prospective shadow/paper process only after execution and promotion work is authorized. Broad research coverage does not imply simultaneous activation of 48 strategies.

## Time-ordered evaluation and leakage controls

Proposed default: select a sufficiently long, causally valid data span, then freeze the earliest 60% for development, next 20% for validation, and final 20% as a locked test. Exact dates must be chosen from data coverage and regime requirements **before** performance inspection. Daily and intraday histories will not necessarily have identical spans; sparse events may need much longer calendar coverage.

Within development, use expanding or rolling walk-forward folds. Fit scaling, universe filters, regression coefficients, pairs/cointegration selection and thresholds only on each training fold. Purge training labels whose holding intervals overlap validation; use an embargo appropriate to the maximum overlapping label horizon. Do not shuffle time, reuse revised earnings data as first-release values, or use a future member of an ETF/index. Cross-asset validation alone does not remove shared market-regime dependence.

Make validation choices once using a predeclared ranking. Evaluate the frozen finalist once on the locked test; if changed afterward, that test becomes development evidence and a new untouched period is needed. Forward paper observations are a new time segment, not a substitute for causal historical evaluation. Freeze costs and mark assumptions before looking at that segment.

## Multiple testing and robustness

Maintain one trial ledger across all agents and families. Report the number of candidates, parameter settings, universes and rejected attempts. Related rules such as EMA, MACD and trend timing are correlated hypotheses, not independent confirmation. Broad factor searches need stricter evidential treatment than a single unselected test. [Harvey, Liu and Zhu](https://www.nber.org/papers/w20592).

Use block-resampled expectancy and benchmark differences; apply a predeclared multiple-testing procedure at the family and overall levels. The Deflated Sharpe Ratio is a useful diagnostic for selection and non-normal returns, with its inputs and effective trial-count assumptions disclosed. Probability-of-backtest-overfitting diagnostics can supplement the search audit; neither metric repairs leakage, unrealistic fills or repeated peeking at a holdout. [Bailey and López de Prado](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf), [Bailey et al.](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf).

Perturb parameters locally, delay entries, remove the best few trades/days, separate volatility/trend/liquidity regimes, test post-publication periods where available, and assess asset concentration. Inspect the worst gap, longest loss streak, stop slippage and loss under correlated market shocks. A narrow optimum, a single asset/event carrying the result, or a performance collapse under modest cost changes is evidence against promotion.

## Costs, execution and learning

Maintain three distinct economics views:

| View | Purpose | Treatment |
|---|---|---|
| Execution-price gross | Explain the actual or simulated entry/exit economics | Includes the effects already embedded in fills; avoid a second deduction of the same spread/slippage. |
| Modeled net | Offline ranking and prospective conservative estimates | Versioned commission, spread, slippage, borrow, funding, roll and currency conversion assumptions as applicable. Clearly label estimated costs. |
| Actual net | Final attributable paper ledger outcome | Requires supported fee/cost evidence. Unknown actual cost stays pending; estimates never overwrite it. |

Daily aggregate fee reporting is appropriate for the operator. The learner still needs a causal per-trade **economic target**. For research, a declared allocation/cost model can produce estimated net labels without pretending they are broker-allocated actual costs. For actual-net learning, wait for authoritative costs. Compare gross-target and modeled-net-target candidates on the same out-of-sample cost-adjusted evaluation; do not silently change the currently commissioned gross target or erase its rejected training attempts.

Use quote spreads and executable market depth when available. Otherwise label conservative assumptions, show a break-even cost and run base/2×/3× cost stress as research sensitivity. Do not assume a touched limit order filled, full capacity at the top quote, or zero market impact. Alpaca explicitly describes simulator omissions such as latency slippage, queue position and market impact; its general documentation is not grounds to erase the real account fees Elrond has retained. [Alpaca paper-trading specification](https://docs.alpaca.markets/us/docs/paper-trading).

For fractional equity execution, reconcile order-level rounding separately from price-return economics. For crypto, model both sides' fees and asset-denominated fee debits; see the market roadmap. For FX/futures/pairs, include financing, roll, borrow availability, collateral, margin and multi-leg execution before interpreting a backtest. These products are not authorized for deployment by this research.

## Benchmarks and the result record

Use cash/no-trade, exposure-matched buy-and-hold, existing SMA/Donchian/momentum benchmarks, and a seeded timing/random-entry control with identical exits, costs and exposure limits. A market-neutral strategy needs a relevant residual/factor comparator, not only SPY. Match exposure and turnover when interpreting alpha; taking more market risk is not evidence of a better signal.

Each report should contain rule/source/data/code hashes; all trial IDs; dates and universe; opportunities, rejections, orders, fills and closed/open trades; gross, modeled-net and actual-net status; win/loss/breakeven counts; confidence intervals; average win/loss and payoff ratio; expectancy; profit factor; exposure and turnover; realized/unrealized P&L; drawdown, drawdown duration and tail loss; costs by component; benchmark differences; per-regime results; and unresolved evidence.

Proposed promotion requirements are: no data/causality defect; positive out-of-sample modeled-net expectancy with selection-aware uncertainty; acceptable robustness and cost sensitivity; and drawdown/exposure within numerical limits approved before testing. A 60–70% win rate alone never passes. No numeric risk/drawdown or allocation limits are approved by this document. If evidence is weak, record INCONCLUSIVE rather than retraining until the chart looks good.

## Resources and reproducibility

Use one immutable market-data cache and a small synthetic execution fixture suite. Do not clone the production ledger for strategy tests. Limit initial compute to one coordinator and at most two bounded evaluation workers after authorization; cap job time, RAM and output retention explicitly at implementation. Successful synthetic test scratch is disposable; research manifests, aggregate results, negative outcomes and required reproducibility evidence are retained. No broad backtest, strategy implementation or runtime change was performed for this catalog.
