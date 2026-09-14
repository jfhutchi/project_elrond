# Intraday research: late-day momentum is the next candidate

Research date: 2026-09-14. Status: literature-supported mechanism, locally unvalidated.
No new strategy returns were measured, no orders were submitted, and no trading settings changed.

## Recommendation and evidence

Prioritize a SPY late-day momentum study. This is a research priority based on the evidence and
Elrond's constraints, not a finding that it is the most profitable strategy. It fits the requested
minutes-to-hours holding period and can be expressed without volume rankings, leverage or shorts.

Three mechanisms were compared before choosing this direction:

| Candidate | Published support | Fit and unresolved issue | Decision |
| --- | --- | --- | --- |
| Late-day momentum | Gao et al. find early-day SPY returns predict the final half-hour. Baltussen et al. extend the mechanism across futures markets. | Few decisions, liquid underlying, price-based signal. Costs and transfer to long-only SPY need testing. | First research priority |
| SPY noise-band momentum | Zarattini, Aziz and Barbon report profitable intraday trading outside a time-varying normal-movement band. | More decisions, volume-dependent exit variant, and materially different sizing/execution assumptions. | Reserve candidate |
| Volume-filtered stock opening breakouts | Zarattini, Barbon and Aziz report strong historical results for stocks selected by unusual opening volume. | Requires a broad historical stock universe and consolidated opening volume. Our ten-ETF, 15-minute bundle cannot reproduce it. | Defer pending data feasibility |

[Gao et al., *Market intraday momentum*, JFE 2018](https://www.sciencedirect.com/science/article/pii/S0304405X18301351)
study SPY over 1993–2013. Their opening signal includes the overnight move from the previous close;
using only the first half-hour after the open would change it. Reported predictive R-squared is
1.6%, not a trade return or win probability. This supports investigating predictability, not
assuming a modern executable edge.

[Baltussen et al., *Hedging demand and market intraday momentum*, JFE 2021](https://academicweb.nd.edu/~zda/intramom.pdf)
find that the move from the previous close to the start of the final half-hour predicts that
half-hour across more than 60 futures. They link the effect to hedging demand and generally find
this signal stronger than the opening signal. Their main strategy results exclude transaction
costs, with a separate low-cost S&P futures illustration. Futures portfolios and long/short returns
cannot be imported as evidence for our long-only SPY adaptation. Dealer positioning is a proposed
mechanism; Elrond has not measured it and must not label days as negative-gamma days from price alone.

[Zarattini, Aziz and Barbon, *Beat the Market*](https://alexandria.unisg.ch/server/api/core/bitstreams/a99aba00-f967-49b3-aceb-f544dc386e0b/content)
use the preceding 14 sessions to construct time-of-day movement bands. Their study includes
volatility sizing capped at 4x and assumes $0.0035 commission plus $0.001 slippage per share.
Those inputs do not describe Elrond's fractional, unlevered paper execution. Parameter and
calendar comparisons also create selection concerns. A replication must freeze the base rule,
not copy the best variant found in those comparisons.

[Zarattini, Barbon and Aziz, *A Profitable Day Trading Strategy for the U.S. Equity Market*](https://alexandria.unisg.ch/server/api/core/bitstreams/3c2989c4-688d-4d78-8a71-f02690990d51/content)
study approximately 7,000 stocks over 2016–2023 and select the top 20 by relative opening volume.
They use five-minute openings, both trade directions, ATR stops and per-share commissions.
This is materially different from Elrond's failed 30-minute ETF breakout. It neither validates
that failed rule nor justifies selecting today's winning stocks as a historical universe.

Counterevidence matters: the publisher's indexed abstract of
[*High-frequency momentum and contrarian strategies in U.S. blue chips*, 2025](https://businessperspectives.org/journals/investment-management-and-financial-innovations/issue-493/high-frequency-momentum-and-contrarian-strategies-in-u-s-blue-chips)
reports that apparent benchmark outperformance disappears after reasonable costs. The full page
was access-blocked during this review; this is an abstract-level finding, not an audited replication
of its methods. It reinforces the need for net-cost comparisons, without refuting every intraday rule.

## Elrond feasibility, measured before any new return test

Read-only queries of the active paper store found the prior 632-trade opening-range result and
no other matching intraday/late-day/opening-range memory record. This keyword search is not a
proof that every older experiment is novel; REFUTED.md was also reviewed. Its daily reversal
failure is a warning about costs, not a test of this distinct late-day mechanism.

The new branch's registry reports 69 accumulated trials. A prospective single new experiment
would be trial 70. Comparing publications without measuring strategy returns has not charged a
new return experiment. Any later measurement must first persist its own attempt and dataset use.

An existing 121-session intraday bundle is suitable for engineering checks, but weak for inference.
For planning, assume correlation 0.10, minimum 0.05, one SPY observation per session and independent
sessions. These are explicit project assumptions, not estimated returns or a profitability claim.
The existing power screen requires **848 sessions** and labels 121 **UNDERPOWERED**; the smallest
detectable correlation in that sample is approximately **0.262**. Missing usable predecessor closes
or other data exclusions would reduce the 121-session upper bound further.

The project's screen uses an expected-maximum luck threshold, not an 80%-power family-wise test.
A separate Gaussian Fisher approximation with one-sided family alpha 0.05/70 and 80% power needs
**1,617 independent sessions** for correlation 0.10. Serial dependence increases the requirement.
Neither calculation establishes net profitability; an economic test needs its own paired-return
effect and variance assumptions. Ten correlated ETFs do not multiply independent days by ten.

The registry returns EXHAUSTED for a broad 2016–2026 historical-window query, citing legacy cycles.
That conservative overlap status does not prove every individual date was consumed. Specific
reservations and consumption must be audited before defining a holdout. Fetching a finer bar
interval over previously inspected dates does not create an untouched holdout.

Calculation evidence: `D:/Elrond/evidence/intraday-supported-feasibility-20260914.json`.
The checked-in companion JSON preserves the same calculation for review.

## Proposed protocol v1, fixed before inspecting candidate returns

This is a specification for a future experiment, not a registered or deployed trading strategy.

1. Use SPY only, regular full-length exchange sessions, long/cash, no borrowing. Select dates only
   after consumption audit; report data exclusions. Require one-minute bars, previous official
   session close and point-in-time corporate-action handling. The existing 15-minute data cannot
   resolve the proposed entry delay and final-minute exit.
2. At 15:30 New York time, compute the completed price move since the previous official close.
   If positive, simulate entry no earlier than 15:31; otherwise hold cash. Exit at 15:59. These
   timing buffers and the long-only restriction are Elrond adaptations, not published replications.
3. At most one entry per day, allocation capped at the existing experimental 9% of equity, six
   decimal fractional rounding, no reinvestment of unsettled or unavailable cash. Compare both
   normalized sleeve returns and account impact. A future execution path still needs tested
   protection and forced-exit handling; this research protocol grants no order authority.
4. Compare paired daily net returns against cash and an always-long SPY position over exactly the
   same intraday interval and allocation. Also report exposure so market drift is not called alpha.
5. Fix 5bps per side and 10bps stress before measurement. Independently estimate spread, latency
   and fill costs using timestamped observations at these times; do not lower costs merely because
   the strategy fails. The old repository NBBO snapshot is not a current all-in execution estimate.
6. Freeze the rule and charge the attempt before measurement. No symbol, threshold, weekday or
   gamma-proxy sweep. Record all outcomes. Historical results remain exploratory where data was
   consumed. Independent confirmation and forward shadow evidence precede any paper deployment.

Current SIP quotes returned HTTP403 in this session's earlier feed probe; historical SIP and
current IEX were accessible. [Alpaca's data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq)
distinguishes IEX live data from consolidated SIP and permits older SIP history without live
entitlement. A 15-minute-delayed SIP signal must not be treated as available at 15:30. Any use of
IEX for this candidate needs explicit feed-parity and stale/missing-data checks.

## Decision

There is credible literature supporting further investigation of late-day momentum. There is
not yet sufficient Elrond evidence to call the adaptation profitable or enable it. The next
useful work is a precise window/data audit and execution-cost observation design, followed by
one preregistered test if those gates are satisfied. Repeatedly testing variations against the
same 121 sessions would not resolve the present uncertainty.
