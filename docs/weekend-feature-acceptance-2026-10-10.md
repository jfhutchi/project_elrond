# Weekend feature increments and acceptance

This is an implementation checklist, not a deployed-runtime or profitability claim.
Existing PRs #275-277 supply entry diagnostics, accounting refusal and deadline
classification repairs. Do not duplicate those changes or clear an operational
stop as part of a reporting feature.

## Priority and concrete acceptance

1. **Owned-trade accounting and learning visibility (#242, #163).** Reuse the
   immutable exploration journal. Display recorded and currently verified
   feedback separately; show after-fee net P&L, fees, descriptive expectancy,
   profitable/losing/flat episode counts and sample size. No sample is unknown,
   not zero. Later conflicting evidence excludes a receipt without deleting it.
   Synthetic evidence must remain explicitly synthetic in HTML and JSON.
2. **Genuine-data coverage and progress (#252, #83).** Preserve source timestamps,
   upstream gaps, unknown company/filing coverage and attribution to actual
   provider receipts. A running timer is not new forward evidence. Restoration
   requires genuine complete cohorts, not fabricated bars or relaxed gates.
3. **Evidence-earned applied learning (#163, #242).** Distinguish descriptive
   feedback from changes to predictive parameters and measured improvements.
   Existing exploration feedback changes round-robin selection count only.
   Immutable challengers still require preregistered budgets, chronological
   unseen comparison and valid adoption lineage; positive sample P&L grants none.
4. **Account dashboard acceptance (#256, #82).** Keep the existing month grid,
   verified close reader and signed after-flow daily values. The account's valid
   trading-day denominator includes flats; trade-episode percentages are a
   different metric. Missing closes/resets remain excluded and visibly unknown.

## First increment: read-only exploration scorecard

The experiment detail HTML and JSON share `ExplorationScorecard`. It reads the
registered policy and manifest as of the requested time, reuses `verified_feedback`
and the protected owned-lot resolver, and cross-checks fees, P&L and entry-notional
return. Duplicate, stale, synthetic-mismatched or inconsistent receipts cannot
contribute economic metrics. Profit factor is unknown without verified losses;
it is never rendered as infinity. Actual fill-price effects are already included;
modeled slippage is not subtracted again. Account equity and forecast labels remain
separate from owned-trade economics.

The scorecard does not fit, promote, trade, change the strategy, write the ledger,
perform migration or grant authority. Existing frozen execution code hashes are
unchanged because the reporting module is separate from execution source.

## Remaining acceptance

- Review source and run required lint/types/frozen aggregate checks on its exact head.
- Deploy only through separately authorized normal rollout; source tests are not deployment.
- Verify genuine later PAPER trades, protected exits, fees and reconciliation.
- Demonstrate a registered successor's benefit on later unseen observations.
- Complete any missing provider/filing coverage with approved existing access.

These open issues remain open until their broader acceptance is actually measured.
