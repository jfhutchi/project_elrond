# QuantBot Project Status

Generated at: 2026-08-17T17:51:50Z

| Field | Verified value |
|---|---|
| Current phase | PAPER_OBSERVATION |
| Current strategy version | 1.2.0 (adaptive-momentum-v1-309894d8d8a5296e) |
| Current broker/environment | ALPACA / PAPER (connection verified 2026-08-17T15:03:29Z) |
| Last successful run | NOT_YET_OBSERVED |
| Current equity | $99.99 |
| Paper trading start date | NOT_YET_OBSERVED |
| Trading days observed | 0 |
| Completed trades | 0 |
| Current drawdown | 0.0100% |
| Open bugs | NONE_OBSERVED |
| Known limitations | Fills are ingested from the broker REST ledger once per cycle; the push trade stream is not held open between cycles; Four research cycles found no configuration of this strategy family that beats SPY buy-and-hold on risk-adjusted return; see docs/agent-runbook.md; No untouched historical holdout remains; this account is now the only clean out-of-sample data available |
| Research underway | Exposure normalisation for asset-class sleeves (not yet pre-registered) |
| Next highest-priority task | Resolve outstanding doctor reasons |

Evidence policy: NOT_YET_OBSERVED is used whenever the durable ledger does not contain authentic forward evidence. Historical backtests never count as paper-trading observations.
