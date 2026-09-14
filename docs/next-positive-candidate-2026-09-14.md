# Next candidate: cost-aware end-of-day reversal

This memo records the next research direction after two negative intraday experiments. It does not
enable trading or claim a positive result. The proposal is selected because it has a distinct
mechanism and a clear way to test the binding problem identified in Elrond: turnover and execution
costs.

## Evidence

[Baltussen, Da and Soebhag, *End-of-Day Reversal* (April 2025)](https://academicweb.nd.edu/~zda/EOD.pdf)
reports a cross-sectional reversal among individual US stocks in the final 30 minutes. The predictor
is each stock's return from the prior close through 3:00 p.m. (ROD3); the dependent return is 3:30
to 4:00 p.m. (LH), with a 30-minute gap to reduce bid-ask bounce. Stocks with the lowest ROD3 returns
outperform the highest during LH. The authors report that the effect is present among the largest,
most liquid and most traded stocks, persists in rolling three-year windows, and is strongest after
negative ROD3 returns. They attribute part of the pressure to end-of-day retail buying and reduced
short selling; their tests find gamma hedging is only a partial explanation.

Their value-weighted low-minus-high spread is 3.78 basis points per day (6.38 equal-weighted), before
the paper's own discussion of turnover costs. The long low-ROD leg alone is about 3.55 bps/day in the
illustrative value-weighted sort. That matters for Elrond: the current baseline assumes 5 bps per
side, or 10 bps round trip. A direct long-only copy therefore does not clear the cost floor on the
published average. The paper itself says more efficient execution or trading already-planned
rebalances may be required. This is a reason to investigate cost-aware implementation, not a reason
to lower Elrond's cost assumptions.

The same paper's mechanism differs from Elrond's failed tests. The prior opening-range experiment
was a time-series breakout across ten ETFs. The late-day experiment was a time-series long/cash SPY
signal. This candidate is cross-sectional, stock-specific and asymmetric: it buys losers, skips
winners and can rank only a liquid, broad universe. The repository's earlier daily reversal result
does not refute this 30-minute stock-level mechanism, but it does warn that a reversal that looks
large before costs may be uneconomic after turnover.

Counterevidence is included. The indexed abstract of
[Laly, *High-frequency momentum and contrarian strategies in U.S. blue chips* (2025)](https://businessperspectives.org/journals/investment-management-and-financial-innovations/issue-493/high-frequency-momentum-and-contrarian-strategies-in-u-s-blue-chips)
reports that apparent high-frequency outperformance disappeared after reasonable transaction costs.
The publisher page was access-blocked during this review, so this is an abstract-level caution, not
an audited replication of its methods.

## What would make the test meaningfully different

The first test should not sweep thresholds, time windows, filters or weighting schemes. Freeze one
version before viewing returns:

1. Use a survivorship-free universe of US common stocks with a price floor of $5 and a liquidity
   floor defined from prior-day dollar volume. A broad ETF-only universe cannot test the mechanism.
2. At 3:00 p.m. ET, compute each stock's raw return from the prior official close. Select the bottom
   decile among names with valid data; buy only the loser leg at a delayed 3:31 p.m. reference and
   exit at 3:59 p.m. Use no shorts, leverage or options. A price floor and delayed fill are declared
   implementation constraints, not research-tuned parameters.
3. Use value-weighted allocation within the selected losers, cap each name at the existing 9% of
   equity, cap the sleeve at 90%, round fractional quantities down to six decimals and preserve
   settlement constraints. At $500, the minimum notional must remain above the broker's order floor.
4. Compare against an always-long eligible-stock portfolio and a cash baseline over the identical
   30-minute interval. Report the gross loser-leg return, cost break-even, net account return,
   turnover and the fraction of days with no eligible names. A relative win against a falling
   benchmark is not enough.
5. Fix 5 bps per side and 10 bps stress before measurement. Add a separately measured quoted-spread
   sample for the selected names; do not substitute that sample for fees, impact, latency or queue
   risk. The strategy must clear both the cost floor and a predeclared minimum practical return.
6. Require point-in-time symbol membership, delisting handling, corporate-action coverage and a
   calendar audit. The 2016–2026 daily SIP window is already consumed; a new result there is
   exploratory. Untouched forward paper observations are the only route to confirmation.

## Data feasibility

The current 15-minute ten-ETF bundle cannot test this candidate: it has neither stock breadth nor
the 3:00/3:30 intervals needed by the published design. The late-day collection proves that Alpaca
can return historical SPY minute bars, but does not establish broad historical SIP entitlement,
survivorship-free symbol membership, or current consolidated quotes. A data-feasibility probe must
come before charging a new return experiment. It should answer: whether the historical endpoint can
deliver the required stock universe within a bounded page budget; whether delisted symbols and valid
membership dates are available; and whether the current paper account can obtain live data for the
chosen names at both decision and exit times.

The read-only probe completed after this memo was drafted. The paper asset endpoint returned 14,259
active US-equity assets. Historical SIP 1-minute bars returned full-day samples for AAPL, MSFT,
NVDA, TSLA, AMC and SPY (391 rows including the boundary convention); corresponding IEX samples
were shorter for several symbols. The current multi-symbol SIP latest-bars request returned HTTP
403, while the IEX request returned all six symbols. This supports broad historical collection,
but not real-time consolidated SIP execution. The active-asset count is not a survivorship-free
historical universe: delisted names and membership dates still require a separate source or audit.
Evidence: `D:/Elrond/evidence/eod-reversal-data-feasibility-20260914.json`. No return experiment or
trial charge was made by this probe.

The proposed research is therefore **LITERATURE_SUPPORTED / DATA_FEASIBILITY_REQUIRED**, not
POWERED, SURVIVED or DEPLOYABLE. It should be rejected rather than simplified into ten ETFs if those
data requirements cannot be met. A smaller universe would be a new hypothesis and must be registered
as such.

## Decision

This is the best next candidate found in the current review because it has a distinct stock-level
mechanism and a documented loser asymmetry. Its published average is too small to assume profitability
under Elrond's current cost model. The next action is a read-only breadth and execution-data audit,
followed—only if the audit passes—by one frozen, pre-cost-gated exploratory measurement. No paper
orders, live orders, strategy-config changes or kill-switch changes follow from this memo.
