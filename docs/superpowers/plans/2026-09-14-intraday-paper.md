# Intraday Paper Research Implementation Plan

**Goal:** Investigate and, only with supporting evidence and intact operational controls, automate
long-only paper trades lasting minutes to hours, normally flat before the exchange close.

**User direction:** On 2026-09-14 the user selected intraday holdings of minutes to hours instead
of 1–5 days. They want short-term net returns, not a fixed number of positions. Profit is an
objective, never a guarantee. Real-money trading remains unauthorized.

**Architecture:** Use a separate intraday research model with exchange session boundaries,
completed 15-minute bars, next-bar execution, conservative costs and explicit forced exits.
Do not reinterpret the existing 252-day signals as minute signals. Evaluate one frozen candidate,
record its trial before inspecting returns, and preserve negative results. A failed candidate
must not become a trading strategy merely to increase activity.

**Initial candidate, frozen before return measurement:** Opening-range breakout, long only.
First 30 minutes define the high and low. After the range closes, a completed 15-minute close
above the range high permits entry at the next available consecutive bar's open, until 14:30
New York time. One entry per symbol per session. Initial stop is the opening-range low.
Exit on stop (gap-aware), after 90 minutes, or at the open of the final 15-minute regular-session
bar, whichever comes first. Require complete 15-minute regular-session data; report and exclude
incomplete symbol-days. No overnight holdings. Universe fixed to SPY, QQQ, IWM, EFA, EEM, XLE,
XLF, XLK, XLI, XLV. Equal maximum 9% account allocation per symbol, total <=90%, risk per trade
<=0.5% of equity. Starting simulated capital $500. Costs 5bps each side; separately show 10bps
each side as a prespecified stress scenario. No parameter sweep or selecting winners by symbol.

**Data:** Historical SIP 15-minute bars are accessible; current SIP quotes return HTTP403.
Current IEX quotes are accessible. Feed coverage is a limitation, not equivalent market-wide data.
Use immutable external research data/artifacts, not the live daily-bar table. Complete sessions
only, raw prices (no retroactive adjusted price leakage). Sessions are the sample unit.

## Tasks

- [x] Implement/test deterministic intraday research simulation: completed-bar information,
  next-open fills, gap stops, holding limit, exchange early-close handling, costs, incomplete data,
  sizing and capital constraints. Trusted static code only; no generated executable strategy.
- [x] Fetch bounded historical intraday data; persist exact input hashes and pre-measurement
  candidate/trial identity. Run the frozen candidate and stress case, preserving all outcomes.
- [x] Review evidence. If unsupported, retain the current deployed strategy and report precisely
  why this candidate cannot support the requested automation. No automatic exploration/promotion.
- [ ] If supported, design/test the broker-connected intraday path using the existing paper
  identity, order-intent ledger, reconciliation, risk/protection, duplicate prevention and close
  deadline. Do not change the running strategy until this path and deployment are verified.

The conditional deployment step was not reached: this frozen candidate lost 4.21% at modeled
5bps costs per side and 9.50% under the predefined 10bps stress. See
`docs/intraday-research-2026-09-14.md`. No intraday execution mode is deployed.

## Initial facts

At investigation the paper account held 10 ETF positions, approximately $493 equity and $2.04
cash. The active strategy uses daily bars and monthly roster selection. The latest autonomous
research task was UNDERPOWERED (12 clean observations vs 210 required), and the prior shorter
daily reversal study failed after costs. These findings do not establish an intraday edge.
