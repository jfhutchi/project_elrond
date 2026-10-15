# Outcome-first PAPER exploration

> **Runtime handover, October 15 at 08:13 Eastern:** both stores are now schema 36; upgraded dashboard, inherited protection and deadline services are running. The bounded deployment task waits for the original IWM exit before commissioning guardian/outcomes and enabling trader after fresh protection checks. Trading is not active at this capture. See the [measured runtime handover](../evidence/paper-core/runtime-handover-2026-10-15/README.md).

## Approved hybrid successor

The [daily PAPER specification](../superpowers/specs/2026-10-14-daily-paper-exploration-design.md)
extends the merged [PAPER core](../paper-core.md). It is merged to main in PR #290 and locally installed, but not commissioned:
The successor is implemented and locally tested, including the combined trading-to-learning witness. [Handover](../evidence/paper-core/handover.md) records integration/preparation
status; the measured staged package remains the older XLE release.

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
Retain 0.5% position value, 0.25% entry risk, 1% stop, 30-minute horizon and existing buffers.
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
