# QuantBot Project Status

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
