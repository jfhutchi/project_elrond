# Late-day SPY test: no basis for deployment

The authorized fixed long/cash rule was measured once on 2016-01-04 through 2026-08-21.
There were 2,674 exchange sessions, 2,611 eligible sessions and **1,444 simulated trades**.
No orders were submitted. This is consumed-history exploratory evidence, not forward paper trading.

| Cost per side | Account return | Ending hypothetical $500 | Same-window always-long benchmark | Sampled drawdown |
| --- | ---: | ---: | ---: | ---: |
| 5bps | -12.49% | $437.53 | -21.85% | 12.51% |
| 10bps, predefined stress | -23.14% | $384.28 | -38.20% | 23.14% |

At baseline costs, 340 trades won (23.55%). Each entry used at most 9% of account equity.
These are total returns over the full period, not annual returns. The benchmark enters every
eligible day at the same times/allocation; it is not ordinary SPY buy-and-hold. Relative improvement
includes avoiding some round-trip costs and does not establish an absolute profit.

Independent calculations from the original minute bars matched every fill reference, sizing cap,
trade P&L and final equity. Before costs, the selected trades averaged **-0.271bps per trade**.
A hypothetical fully allocated sequence of those gross trade returns compounded to **-4.30%**;
that normalized sleeve diagnostic is not the cash-constrained account return. Its geometric
break-even symmetric cost was **-0.152bps per side**. Thus smaller spreads alone cannot establish
an edge for this selected sequence. No zero-cost variant was selected or retested.

## Fixed rule and scope

SPY only, full regular sessions. At 15:30 New York time compare the completed 15:29 minute close
with the preceding session's raw vendor daily close. If positive, enter at the 15:31 minute open
and exit at the 15:59 minute open; otherwise stay in cash. Fractional quantities round down to six
decimals. No shorts or borrowing. Proceeds remain unavailable until the second subsequent exchange
session, a conservative uniform settlement assumption. This long-only adaptation and its timing
buffers differ from the [published mechanisms](intraday-supported-candidate-2026-09-14.md).

## Data and execution checks

Historical collection used 214 minute-data pages and two daily-data pages. The saved bundle contains
the three required minute observations per day, daily references, corporate actions and calendar.
No dates after 2026-08-21 were fetched for this experiment. Independent `exchange_calendars` 4.13.2
matched all 2,674 broker session dates and opening/closing times. The audit is bound to the input
hash and immutable measurement manifest.

Exclusions: 40 reported corporate-action dates, 21 shortened sessions, one initial date without a
previous close and one missing required minute. Corporate-action coverage remains unverified: the
first returned SPY ex-date is 2016-06-17. Raw/adjusted factor comparisons are an extra check, not proof
of complete history when both vendor feeds could share omissions.

Before measuring returns, QC discovered cent rounding in adjusted closes versus four-decimal raw
closes. Exact factor comparisons falsely excluded 1,060 sessions. A tested, independently reviewed
correction uses half-cent adjusted-price uncertainty intervals before applying the original relative
tolerance. Signal parameters and costs were unchanged. This QC correction is recorded in the plan.

Raw daily closes are vendor proxies for official exchange closes. Revised historical bars and
adjustment-based exclusions are not demonstrated point-in-time observations. Minute opens are fill
references, not executable quotes. Drawdown is sampled after daily exits and misses intrabar losses.
The descriptive HAC error uses five eligible-observation lags, compressing excluded sessions; it is
not a confirmation statistic or a promotion gate.

A prespecified quote sample used the first full session of each month in 2025 at 15:31 and 15:59.
All 24 SIP observations were valid. Median quoted half-spread was 0.084bps, maximum 0.257bps.
This measures neither latency, impact, fees nor fractional fill quality. It does not justify
replacing the fixed 5/10bps assumptions or establish live SIP access.

## Durable evidence and verification

Implementation commit `f928dc9` preceded measurement. One attempt was committed before simulation;
the permanent trial count rose from 69 to 70. Both cost cases are preserved. Research memory records
the result as EXPLORATORY_ONLY without promotion authorization.

- Search ID: `intraday:f858d6446d6b3acd2b23070df3b356c9313eb5ae94a33ba34e2d6d9fc7fc6a08`.
- Inputs: `D:/Elrond/evidence/late-day-inputs-20260914.json`.
- Input hash: `8516ce7ec781feedb8036d0cf5941310230f402e1b21ffc998e0f53449c19436`.
- Calendar audit: `D:/Elrond/evidence/late-day-calendar-audit-20260914.json`.
- Manifest: `D:/Elrond/evidence/late-day-results-20260914.manifest.json`.
- Full outcomes: `D:/Elrond/evidence/late-day-results-20260914.json`.
- Result-file hash: `b66360df3f2862cdf10494cfdb1f9bad9c82b76091060e4485cd34f0e4bd0b5f`.
- Quotes: `D:/Elrond/evidence/late-day-quotes-20260914.json`.
- Independent arithmetic: `D:/Elrond/evidence/late-day-verification-20260914.json`, copied into
  `docs/late-day-verification-20260914.json` for GitHub review.

Twenty-three new synthetic tests passed; Ruff and mypy passed. Independent review approved the
path and audit gate before measurement. The prior full Windows CI applies to `1347355`, not this
later standalone script; it is not represented as a fresh full-suite run.

`scripts/late_day_research.py collect` requires `--env-file` and `--bundle`; `measure` requires
`--bundle`, `--calendar-audit`, `--database` and `--output`. Measurement reads no credentials and
makes no network calls. Existing evidence/attempts block automatic replay, including interruption
after charging. Do not delete trial rows to rerun. Reproduce pure arithmetic from saved inputs in
an isolated test process without the broker or active store.

## Decision

Do not deploy this adaptation. Preserve this negative finding alongside the literature support.
A future candidate needs a distinct economic justification or better data and its own declared
trial charge. Tuning exit times or filters until this same history looks profitable would not
provide the missing independent evidence.
