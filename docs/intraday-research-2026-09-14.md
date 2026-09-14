# Intraday paper research: first candidate did not support deployment

The user requested automated paper trades lasting minutes to hours, normally closing before
market close. This report records a real test of that direction. No intraday broker orders were
submitted, existing holdings were not sold, and the active daily strategy was not changed.

## Result

One frozen long-only 30-minute opening-range breakout rule was evaluated on 121 regular trading
sessions from 2026-03-02 through 2026-08-21. The fixed universe was SPY, QQQ, IWM, EFA, EEM,
XLE, XLF, XLK, XLI and XLV. All 31,460 regular-session 15-minute bars were present; no symbol-day
was excluded. Signals used completed bars and filled at the next bar's open. Positions exited
within 90 minutes or by the opening of the final 15-minute session bar, whichever came first.

| Adverse cost per side | Net return | Final simulated $500 | Sampled maximum drawdown | Trades |
| --- | ---: | ---: | ---: | ---: |
| 5 basis points | -4.21% | $478.96 | 5.03% | 632 |
| 10 basis points, predefined stress | -9.50% | $452.50 | 9.92% | 632 |

At baseline costs, 269 trades won (42.56%). Exits were 590 holding-time limits, 35 stops and
7 session-end exits. No symbol entered more than once per session.

This candidate provides no basis for enabling intraday trading. It is EXPLORATORY_ONLY, not a
formal refutation of all intraday strategies. Faster turnover does not establish a most-profitable
action. Existing research and promotion controls remain applicable.

## Timing and limitations

One chronological cash ledger is shared across all ten symbols. Each entry is capped at 9% of
current equity, aggregate entry exposure at 90%, and estimated initial stop risk including costs
at 0.5%. Gaps can exceed initial stop-risk estimates. Quantities round down to six decimal places.
An intrabar stop is timestamped at bar end because OHLC does not reveal the actual touch time;
its proceeds cannot fund entries at that bar's open. Sampled drawdown may miss an intrabar low.

Raw historical SIP bars do not establish actual fills, spreads, liquidity, latency or queue
position. Historical SIP access succeeded; current SIP quotes returned HTTP403, while current
IEX quotes were accessible. A future execution design must account for this difference. Alpaca
documents additional [paper/live differences](https://docs.alpaca.markets/us/docs/paper-trading).

The discovery window is already covered by historical consumption. No later session was included.
One attempt was committed before either cost case ran. Both cases were fixed beforehand and both
losses were preserved. New registry accounting retains this trial even without registration and
credits at most one measured descendant for its prepaid attempt.

## Evidence

- Implementation commit: `1347355` (code and frozen plan).
- Input: `D:/Elrond/evidence/intraday-20260914-inputs.json`.
- Input SHA-256: `25216e21eed45919f81ae4fa0bf5118f2b4ce6cecfa63409fa98b6fc6f8d721e`.
- Full results: `D:/Elrond/evidence/intraday-20260914-results.json`.
- Search ID: `intraday:34ca3a3dfb0a3e27d20adc82d007e53d5e3bb6f28b320e1b3059f5e8ce27918a`.
- Independent review: `D:/Elrond/evidence/intraday-review-20260914.md`.
- Durable research memory source: `intraday-research/v1`, verdict EXPLORATORY_ONLY.

`scripts/intraday_research.py collect` saves immutable historical inputs; `measure` requires an
explicit bundle, database and output and records the trial first. Existing results or previously
charged identical attempts are refused, including interrupted attempts. Do not delete a ledger
entry to retry. For arithmetic reproduction, load the preserved data and call `simulate` in an
isolated test process without overwriting original evidence or making a new selection.

Focused verification: 39 tests passed, Ruff passed, and mypy passed. Independent review found no
Important/Critical defect in the specified historical path. The full local Windows CI job passed
against implementation commit `13473554862e4b1610891b42db295164bfbc979b`: 2,988 tests passed,
47 skipped and one warning; lint and both Linux-targeted and Windows-targeted type checks passed.
Linux execution was NOT RUN because Docker is unavailable on this host, so the aggregate
`--platform all` command exited 1 despite the Windows job passing. Skips include unavailable
PostgreSQL/WSL facilities and platform-specific cases; this is not cross-platform execution proof.
The CI summary is preserved at `D:/Elrond/evidence/intraday-ci-20260914-summary.json`, with the
Windows log beside it as `intraday-ci-20260914-windows.log`. Subsequent commits change only docs.

## Deployment decision

Do not deploy this losing candidate. A future candidate needs a declared mechanism, a trial charge,
realistic execution assumptions and independent evidence. The conditional broker-connected intraday
implementation was not reached. No intraday trading mode is deployed.
