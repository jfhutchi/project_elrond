# Noise-band SPY research plan

The user requested continued honest research after two failures. This candidate was listed as
a reserve in the literature review before the second test. Economic rationale: distinguish
an unusually persistent intraday move from the normal movement at that time of day.
Source: Zarattini, Aziz and Barbon, https://alexandria.unisg.ch/server/api/core/bitstreams/a99aba00-f967-49b3-aceb-f544dc386e0b/content.

Frozen v1 before measurement: SPY long/cash, one entry maximum per session, 14 preceding exchange
sessions, multiplier1, checks10:00,10:30,...15:30, one-minute-delayed fills. At each check compute
mean(abs(prior completed price at same time / prior09:30open -1)) over exactly14prior sessions.
Upper band=max(today09:30open,previous vendor daily close)*(1+mean_move).
First check whose completed price exceeds the band enters at next-minute open. Exit at the next
check at/below its current band, filled one minute later, or15:59open. No reentry, shorts, leverage
or VWAP. These are explicit retail/cash adaptations, not a replication of the published final
long/short, leveraged/VWAP result. Reference: always enter10:01 and exit15:59 on eligible days.

Use existing9%equity,$500,T+2settlement,six-decimal rounding,5bpsper-side baseline/10bpsstress.
No cost tuning. No parameter/weekday/regime/symbol sweep. Return measurement charges exactly
one additional attempt before execution, preserving both cases and all selected trades.

Dates2016-01-04..2026-08-21, whollyconsumed; no confirmation claim. Reuse hashed daily references,
corporate actions and independently audited calendar from the late-day bundle. Download needed
one-minute signal/entry/open/exit observations only; retain raw values. Require14 immediately
preceding full sessions with all needed observations, excluding corporate-action/missing/short
sessions and their lookback windows. Exclusions are conservative retrospective data hygiene,
not proof of point-in-time availability. Historical revised daily close proxy caveats persist.

## Work

- [ ] Test prior-only band, overnight-gap anchor, delayed fills, first-entry restriction, stop,
  flat-at-end rule and missing lookback rejection using synthetic prices.
- [ ] Implement standalone script using tested late_day_research cash/accounting functions.
  Bind both code hashes, inputs, parent bundle and calendar audit in the measurement manifest.
- [ ] Collect bounded historical observations, review code, freeze before outcomes and measure once.
- [ ] Independently verify trade arithmetic, preserve evidence/memory and publish honest result.

Diagnostics fixed beforehand: full-window net/gross returns, trades, benchmark and drawdown;
2025-01-01..2026-08-21 recent slice is descriptive only, not a newholdout. This slice is motivated
before testing by an independent public replication reporting recent deterioration. That external
claim has not been independently audited and does not supply local performance evidence.

Further exit-time optimization of the failed late-day rule is deferred. Published closing-auction
mechanisms may differ from exiting15:59, but trying many exits on inspected history would add
selection freedom. This independent candidate is one bounded experiment, not a search for a green line.
