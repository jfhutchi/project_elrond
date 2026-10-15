# Outcome-first PAPER exploration

> **Current PAPER runtime, October 15:** The approved affordable-stock/whole-share entry policy is active on the Omen. Fractions remain preferred; an exact one-share fallback keeps the existing loss budget. The first AAPL round trip is verified; fees/net remain unknown and no learner fit is claimed. See the [current handover](../evidence/paper-core/whole-share-2026-10-15/README.md).

## Approved hybrid successor

The [daily PAPER specification](../superpowers/specs/2026-10-14-daily-paper-exploration-design.md)
extends the merged [PAPER core](../paper-core.md). The hybrid successor is commissioned locally;
the affordable-stock/whole-share amendment is active from its reviewed feature branch. The
[current handover](../evidence/paper-core/whole-share-2026-10-15/README.md) records exact pins,
the first actual AAPL round trip and the remaining whole-share/learning acceptance limits.

`SIGNAL_OR_EXPLORE_V1` evaluates AAPL, MSFT, AMZN, GOOGL, META, NVDA, JPM, JNJ, XOM and PG,
with SPY as benchmark only. Any valid activated prediction strictly above 10 bps takes
priority, including a used stock over unused weak candidates. Rank signals by current-session
reservation count ascending, prediction descending, prior-session count ascending and configured
order. Only when no eligible signal qualifies, rank exploration by current-session count,
prior-session count and configured order. Preserve the true weak/negative score and record
`EXPLORATION`; COLLECT explores, invalid model authority refuses. Unavailable/inherited-held
members do not suppress healthy candidates. Claim and final send replay cohort selection.

Enforce six durable reservations per verified exchange session and one concurrent core lot.
Completed and possible-send/unknown reservations count; only definitive pre-send proof releases
one. A semantic bar boundary does not change with acquisition time, symbol, model or release.
Retain the base 0.5% position value, 0.25% entry risk, 1% stop, 30-minute horizon and buffers.
The approved optional `WHOLE_SHARE_FALLBACK_V1` adds F, T, PFE, VZ, HPQ, SOFI and UBER.
Use fractions when explicitly supported; otherwise floor to whole shares with a minimum
of exactly one. Only this minimum may exceed the notional cap; it must still fit current
cash, 95% of buying power and the unchanged planned-loss budget. Ordinary amendment sizing
uses 99% of the normal allocation as equity-mark headroom. Recheck typed eligibility at
actual send, refuse capability changes, and retain receipt provenance. Unknown is not false.
The explicit flat/fenced entry-policy transition preserves base economics, old ownership,
models, daily counts and all compatible history. Do not re-commission or reset a store to
apply it. See the [design](../superpowers/specs/2026-10-15-whole-share-fallback-design.md).
Six reservations do not guarantee six fills. Guardian marks follow owned symbols; protection/
outcomes continue after the cap or a failed fit. Do not liquidate inherited holdings for capacity.

V3 learns actual owned gross wins/losses after ten completed exchange sessions, including quiet
ones. Consider daily using all eligible compatible history, latest-ten/older raw weights 2/1
normalized during fitting. Preserve past-only folds, availability, protected exclusions and
30-minute purge/embargo while retaining overlapping feature windows within training. Established
gross outcomes may train when `FEE_UNAVAILABLE` is the sole remaining gap in a closed, owned,
reconciled lot; fees stay unknown, status stays `COSTS_PENDING` and net stays null. V2 remains
strict net. `DAILY_BOUNDED_V3` cannot inherit V2 authority; initial successor commission uses
target-neutral COLLECT. Failed/rejected fits retain the incumbent while exploration continues.

`BROKER_PAPER_BALANCE_V1` is the approved current-funds contract implemented on the successor branch.
Fresh stable PAPER balances and open-order evidence must bind to quantity proof, current ledger
and exact commission at claim/gate/locked send and recovery. Capture is stable within five
seconds and at most 120 seconds old; changed source/ledger/balance needs fresh authority.
It authorizes sizing, not historical cash reconstruction: retain `expected_cash=None`,
`cash_complete=False`, unknown fees and null net. Never invent cash activity, a balancing
residual or reconciliation; preserve discrepancies, unknown sends, manual stops, restrictions
and other genuine gates. Legacy model-only mode remains strict exact-cash.

This economic selection/learning/funds change requires its own successor manifest, configuration
and initial commission. The older XLE verifier and legacy launch commands do not authorize it.
Preserve original client IDs, owners, stores, trial charges and dated evidence.

## Legacy exploration implementation retained for reference

The remaining contract belongs to the earlier experiment coordinator. Its no-quota rotation,
known-fees-only feedback and nonpredictive/no-promotion statements do not govern the approved
hybrid/V3 successor. They remain unchanged to interpret old policies and receipts.

This is an explicitly commissioned, nonpredictive sampling policy. It collects
protected PAPER trade experience without pretending a weak forecast is qualified.
Negative baseline predictions remain unchanged, linked pretrade diagnostics.
Ordinary fitted-model execution retains its cost floor and promotion requirements.

The policy uses a complete, genuine five-minute cohort for the frozen instrument
roster. Selection rotates deterministically through the roster after completed
owned episodes. There is no daily trade quota. Only one concurrent new position is
allowed, with at most 0.5% equity position value, 0.25% entry risk, a 1% stop and a
30-minute horizon. Frozen manifest and existing portfolio controls can be tighter.
Inherited inventory retains its original exit owner and aggregate exposure checks.

The pinned coordinator, actual broker clock, canonical holdout guard, actual cash
and buying power, reconciliation, emergency stop, recorded veto policy and
independent deadline/protection gateway remain mandatory. A claimed uncertain
submission is recovered by the same client ID; it is never released for resending.
Unknown calendar coverage, terminal experiment state or an exhausted evaluation
window blocks new entries while existing protection and recovery remain required.

Real use requires an immutable `ExplorationPolicy`, an operator-attributed
`ExplorationCommission` for its exact code/configuration binding, an existing
registered PAPER deployment and exclusive entry ownership. The launcher does not
create these records, approve a fitted model, change security or submit on import.
Its SHADOW source configuration cannot enable inline fits during initial handover.

Before handover, preserve nonsecret configuration, task definitions and consistent
database backups. Verify clock, reconciliation, protection/deadline freshness,
existing position ownership, absence of unknown pending sends, exact release and
binding. Disable and stop the predecessor entry/coordinator workers before starting
the limited-identity exploration task; retain all independent position protection.
Publish `exploration-selection.json` only from the exact commissioned binding and
existing deployment. Original-learner standby reads a matching journaled real
`TickReceipt`; static metadata is never treated as a completed heartbeat. Abort
before entry if any proof disagrees. Rollback transfers entry ownership back only
after recovery and ownership checks; never rewind databases over later broker
events or erase an exploration episode.

Each entry links its immutable observation, actual intent and owned lot. Feedback
requires actual fills, linked independent protection/exit, terminal orders,
reconciled flat inventory and known fees. Missing evidence remains incomplete and
blocks another episode; it does not become a zero-return day. Actual fill-price
economics and fees are not charged a second time for modeled slippage. Conservative
modeled costs remain separate diagnostics. Comparison to baseline forecasts is
labeled executed-exit comparison, not fixed-target forecasting accuracy.

`ExplorationFeedback` is descriptive experience, not a trained/improved predictor
or statistical evidence of profitability. Successor research still uses registered
fit budgets, canonical training/validation and later unseen forward evidence. No
automatic live switch or model promotion exists. Runtime acceptance requires real
entry, protected exit, attributable fee-complete feedback and subsequent prospective
evaluation; synthetic broker tests alone do not establish that acceptance.
