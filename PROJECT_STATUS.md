# QuantBot Project Status

Generated at: 2026-08-15T18:16:20Z

| Field | Verified value |
|---|---|
| Current phase | PAPER_OBSERVATION |
| Current strategy version | 1.1.0 (adaptive-momentum-v1-b73083b817f76b8f) |
| Current broker/environment | ALPACA / PAPER (connection verified 2026-08-15T13:37:08Z) |
| Last successful run | run-20260815T133708Z at 2026-08-15T13:37:10Z |
| Current equity | $100.00 |
| Paper trading start date | 2026-08-15 |
| Trading days observed | 1 |
| Completed trades | 0 |
| Current drawdown | 0.0000% |
| Open bugs | NONE_OBSERVED |
| Known limitations | Fills are ingested from the broker REST ledger once per cycle; the push trade stream is not held open between cycles; Four research cycles found no configuration of this strategy family that beats SPY buy-and-hold on risk-adjusted return; see docs/agent-runbook.md; No untouched historical holdout remains; this account is now the only clean out-of-sample data available |
| Research underway | Exposure normalisation for asset-class sleeves (not yet pre-registered) |
| Next highest-priority task | Resolve outstanding doctor reasons |

Evidence policy: NOT_YET_OBSERVED is used whenever the durable ledger does not contain authentic forward evidence. Historical backtests never count as paper-trading observations.
