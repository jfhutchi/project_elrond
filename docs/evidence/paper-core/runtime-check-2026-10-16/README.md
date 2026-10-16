# PAPER runtime audit and entry-data correction â€” October 16, 2026

> **Correction, 15:03 Eastern:** The protection-health conclusion below was incomplete: GOOGL’s integrity quarantine persisted and blocked its exit. The [later audited repair](../ownership-recovery-2026-10-16/README.md) records the corrected reporting, concurrency fix, actual 14:55:03 exit and current runtime. Original JSON captures remain unchanged.

Measured at 11:13 Eastern on the HP Omen. This package follows the
[October 15 policy handover](../whole-share-2026-10-15/README.md); that sealed package
remains historical evidence. Source correction: `f934d16` on `hutch/whole-share-fallback`.

## Correction and validation

A production-shaped GET cohort request timed out after 2.078 seconds under the original
two-second allowance. The same request completed in 3.453 seconds with a five-second
allowance. This was a read-latency failure, not evidence of unsupported fractional stock.

Only the trader's market-data read allowance changes from two to five seconds, with a
ten-second total cohort acquisition budget instead of five. Broker requests, calendar reads,
guardian market reads and outcomes market reads retain their two-second limits. No retries
were added. Source-bound 120-second freshness, locked final-send checks, stops, deadlines,
ownership, risk budgets, the single open core lot and six session reservations remain intact.

Offline gates: six new regression cases, 103 trade/cost/learning cases and 23 final-send,
protection and whole-share cases passed: **132 tests**. Independent review passed the six
new cases separately and found no issues. Ruff and whitespace checks passed. The tests
include slow valid data, stale rejection, acquisition timeout and production client wiring.
No paid CI was triggered.

## Deployment and actual trading

Entries were fenced at 11:07 while independent protection continued. AMZN exited naturally
at 11:10:05. Core flatness was verified before stopping the three core workers. Retained
Protection and Deadline stayed running. A consistent SQLite backup passed integrity checks.

The code-only transition installed executable
`f3f65d4023f41fe25e40560fa7eb7727c665f93857343b6fd4e5db16d60e4a1c`
in the current native repository. Runtime configuration, economic commission, manual-stop
state and model/history tables were preserved at the handover. Entries resumed at
11:11:06 using fresh independent guardian, inventory and time evidence. All six services
were Running; the dashboard returned HTTP 200. Current core PIDs were guardian 2120,
outcomes 7576 and trader 8160, each reporting against the new executable.

Today's broker fills:

| Stock | Entry (Eastern) | Exit (Eastern) | Gross P&L |
| --- | --- | --- | --- |
| MSFT | 10:05:43 | 10:35:18 | -$0.006046708 |
| AMZN | 10:40:30 | 11:10:05 | +$0.000328338 |
| GOOGL | 11:11:25 | Open at capture | Unknown |

GOOGL's automatic entry filled **0.007112 shares at $344.346** after the correction and
resume. No manual trade was placed. Guardian is healthy; the trader's occupied-slot wait
and outcomes worker's not-flat wait refer to this open position. A brief fill-ingestion
inventory mismatch cleared on the following independent cycle.

The final GET-only bar probe returned eight completed five-minute bars for every one of
the 17 stocks and SPY. Entry freshness continues to be evaluated at the decision time;
this capture at 11:13 is outside the two-minute entry window for the 11:10 boundary.
Checked asset responses contained explicit boolean `tradable=true` and `fractionable=true`.

## Learning and costs

The verified broker calendar contains **one completed session out of the ten-session
warmup**: October 15. October 16 remains in progress. There are two `NO_CHANGE` assessment
receipts, both citing `TEN_COMPLETED_SESSION_WARMUP`, and **zero fit attempts**. The
active model remains `COLLECT`. Receipt count is not a count of fitted models or sessions.

Three completed round trips, including yesterday's AAPL, pass the approved gross-target
constructor. The guarded selector accepts yesterday's outcome and excludes today's two
closed outcomes only with `SESSION_NOT_COMPLETED`. Today supplies both a negative MSFT
and positive AMZN gross target. No history was deleted or relabeled; all eligible compatible
history remains available with raw weight two for the latest ten sessions and one for older
sessions. Existing sample, validation, protection and compatibility gates still apply.

Every fill retains unknown fees. Completed outcomes retain `COSTS_PENDING/FEE_UNAVAILABLE`,
with actual fees and net P&L null. Two account-level fee activities have no order/fill
references or verified intraday timestamps and cannot establish per-trade charges. They
were not allocated by guess or replaced with zero. The approved gross-return learner can
use these otherwise verified outcomes; verified net performance remains unavailable.

## Evidence

JSON files record the latency reproduction, typed asset fields, final bar probe, validation,
exact patch and binding, writer fence, preservation hashes, resume evidence, service states,
worker health and guarded learning selection. `hashes.json` seals the eleven JSON captures.
Private credentials, raw account identity and SQLite backups are excluded.
