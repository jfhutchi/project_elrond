# QuantBot status: current pointers and historical snapshot

## Current orientation — October 14, 2026 EDT

The three-role [PAPER core](docs/paper-core.md) implementation merged in
[PR #289](https://github.com/jfhutchi/project_elrond/pull/289) on October 15 at 01:50 UTC.
Functional deployment and operational acceptance remain waiting. At the last runtime check,
October 15 at 02:00 UTC, the new tasks were disabled with submission/entry clamps and no triggers,
the operating store remained unmigrated, and an existing accepted IWM SELL of 0.169669 shares
blocked the required no-open-order commissioning condition. Existing protection/deadline services
retain responsibility for inherited positions. The Omen remains the sole permitted PAPER
coordinator; the Pi is retired.

Read the [handover](docs/evidence/paper-core/handover.md) and
[current deployment evidence](docs/evidence/paper-core/omen-deployment/README.md) for measured
state, exact release/config pins, migration rehearsal, and the remaining sequence. The prepared
runtime uses schema 36 and Mako 1.4.3; migration was exercised only on copies. A source merge or
the passage of time does not establish runtime progress.

The latest request adds daily PAPER exploration across different stocks and learning from wins
and losses. That behavior is pending implementation: the prepared configuration selects XLE with
one new position and may abstain. The implemented learner considers all eligible compatible
history after ten completed exchange sessions, including quiet sessions, with daily consideration
and recent-session weighting. No deployed daily learning or improvement is claimed here.

## Preserved generated snapshot — August 18, 2026

The following generated report is historical evidence, retained without changing its original
measurements. Every use of “Current” or “Next” below refers to its generation time, not today.
Its equity, observed days, trade count, strategy, research priority, and qualification window
must not be used as current operating facts. Commands that regenerate this report may replace
this file; the linked handover and evidence remain the durable source for the redesign.

Generated at: 2026-08-18T00:57:32Z

| Field | Verified value |
|---|---|
| Current phase | PAPER_OBSERVATION |
| Current strategy version | 1.2.0 (adaptive-momentum-v1-309894d8d8a5296e) |
| Current broker/environment | ALPACA / PAPER (connection verified 2026-08-18T00:57:30Z) |
| Last successful run | run-20260818T005730Z at 2026-08-18T00:57:32Z |
| Current equity | $100.09 |
| Paper trading start date | 2026-08-18 |
| Trading days observed | 1 |
| Completed trades | 0 |
| Current drawdown | 0.0000% |
| Open bugs | NONE_OBSERVED |
| Known limitations | Fills are ingested from the broker REST ledger once per cycle; the push trade stream is not held open between cycles; Four research cycles found no configuration of this strategy family that beats SPY buy-and-hold on risk-adjusted return; see docs/agent-runbook.md; No untouched historical holdout remains; this account is now the only clean out-of-sample data available |
| Research underway | Exposure normalisation for asset-class sleeves (not yet pre-registered) |
| Next highest-priority task | Accumulate paper observations toward the 30-day qualification window |

Evidence policy: NOT_YET_OBSERVED is used whenever the durable ledger does not contain authentic forward evidence. Historical backtests never count as paper-trading observations.
