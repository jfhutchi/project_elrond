# Late-day SPY measurement plan

Goal: Execute the authorized fixed late-day research protocol without submitting orders.
Spec: docs/intraday-supported-candidate-2026-09-14.md.

Architecture: A standalone research script collects a hashed, bounded SIP input bundle, then
measures without credentials/network access. Existing intraday attempt accounting charges one
candidate before the simulator executes. Results and limitations enter durable memory.

Frozen before return measurement: SPY only; 2016-01-04 through 2026-08-21, wholly within the
legacy consumed range. First date seeds the previous close; full 09:30–16:00 sessions only.
Signal uses the 15:29 minute close (bar completes 15:30) divided by prior-session vendor daily
close minus one. Buy at 15:31 minute open iff positive, sell at 15:59 minute open. Start $500,
9% of current equity maximum per trade, fractional shares rounded down to six decimals.
Sale proceeds stay unavailable until the second subsequent exchange session, a conservative
settlement assumption across the entire sample. Benchmark has the same allocation, costs,
settlement and time interval but enters every eligible session. No leverage, shorts or sweeps.
Costs fixed at 5bps per side plus a separately reported 10bps stress.

Data hygiene decisions before measuring returns: collect raw daily bars and split/dividend-adjusted
daily bars solely to audit adjustment-factor discontinuities. Exclude any session with a material
factor change (>0.00001 relative) and any provider corporate-action ex-date. This avoids trading
across ambiguous dividend/split changes; do not calculate signals from back-adjusted prices.
Flag missing action metadata. Ex-post adjustment-factor exclusions and revised historical bars
are limitations, not proof of point-in-time availability. Daily vendor closes are proxies for
official auction closes and must be labeled accordingly. Missing required bars or daily close,
duplicate timestamps, nonpositive prices or invalid OHLC must never silently create fills.

Pre-measurement QC correction: the collected adjusted daily closes are rounded to cents,
whereas raw closes can have four decimals. Exact factor comparisons falsely excluded 1,060
sessions before any return test. Compare factor intervals using +/- $0.005 adjusted-price
rounding uncertainty, then apply the same 0.00001 relative threshold between non-overlapping
intervals. The dividend exclusion rule and signal are unchanged. Raw daily dates exactly match
the 2,674 broker-calendar dates; validate this against an independent exchange calendar too.

## Tasks

- [x] Write synthetic tests for signal timing, negative/zero signal, costs, fractional allocation,
  settlement, benchmark accounting, early-close/missing/dividend exclusions and malformed inputs.
- [x] Implement deterministic simulation in scripts/late_day_research.py; run those tests and
  static checks. Freeze script identity before measurement.
- [x] Collect historical inputs with page limits, retain only required minute observations,
  verify hashes and count eligible sessions. No data after 2026-08-21 is accessed.
- [x] Persist one attempt and immutable manifest before invoking simulation; preserve both
  fixed cost outcomes, paired daily benchmark differences and sampled drawdown.
- [x] Independently reconcile resulting arithmetic, record memory/report and update draft PR.

Outcome: 1,444 trades across 2,611 eligible sessions lost 12.49% at 5bps per side and 23.14%
at 10bps. See docs/late-day-result-2026-09-14.md. No strategy deployment follows.

Report the strategy's account return, normalized fully-invested-per-trade sleeve compound return,
trade count, win rate, gross break-even cost, benchmark return, paired mean daily difference and
a descriptive lag-5 HAC standard error. These are exploratory diagnostics, not significance or
promotion gates. Full intrabar drawdown, real fills, latency costs and feed parity remain unverified.
Only read-only historical quote observations may be used to illustrate spread; they cannot
establish all-in costs. No profitable result permits deployment without independent evidence.
