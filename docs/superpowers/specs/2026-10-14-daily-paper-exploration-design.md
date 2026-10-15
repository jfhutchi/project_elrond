# Daily PAPER signals, exploration and learning

Date: October 14, 2026, America/New_York. Status: implementation specification for the user's explicit clarification after PR #289 merged. Runtime activation remains separate from implementation. This supersedes the earlier single-symbol/no-quota selection objective; the retained accounting, ownership, protection and real-capital boundaries still apply.

## User intent

The user wants actual broker PAPER experience every trading day across different stocks: use a qualifying model signal when one exists, otherwise make an exploratory trade. Learn from both wins and losses after ten completed exchange sessions, retain all eligible historical data, and give the latest ten sessions more weight. No-signal or low predicted return must not make experience collection optional. Daily improvement is an objective to measure, not a result that code can guarantee.

The direct clarification authorizes the behavior change. The numerical defaults below are project defaults selected to make it concrete, not fabricated operator attestations. A closed market, unavailable broker, invalid data, unknown send or ambiguous ownership cannot be turned into an invented fill. Record the reason, continue independent protection/outcome work, and retry when the dependency recovers.

## Approach

Use the existing three-role PAPER core and durable outbox. Add a configured stock universe, deterministic signal-first/exploration selection, and transactionally enforced daily and bar-boundary reservations. Keep one concurrent core lot, 0.5% equity per entry, a 1% stop and the existing 30-minute exit horizon. The first policy permits six entry reservations per exchange session, including signal and exploration trades.

Only adding exploration to the old trader is insufficient: it remains XLE-bound, the learner discards adjacent feature windows, and unknown fees can exclude every real paper outcome. The selected approach therefore versions the learning procedure as well. A separate simulated-fill engine is unnecessary; synthetic tests remain explicitly synthetic and never become broker experience.

## Entry selection

Add `SIGNAL_OR_EXPLORE_V1` beside the unchanged legacy `MODEL_ONLY_V1` selection behavior. The prepared universe is `AAPL, MSFT, AMZN, GOOGL, META, NVDA, JPM, JNJ, XOM, PG`. These are candidate symbols, not assertions of current broker eligibility; acquisition must verify usable data and respect broker restrictions. SPY remains the declared benchmark and is not a trade-universe member for this feature contract.

At each completed five-minute boundary, when the global core entry slot is available:

1. Acquire a bounded causal cohort for the configured universe plus benchmark. A missing or unavailable symbol has an explicit reason and does not suppress healthy members. Each observation uses actual received data/times, one completed boundary and the correct symbol.
2. Exclude inherited-held symbols, invalid/stale data, scoped quarantine, or other genuine entry failures. Preserve account/store/PAPER identity, guardian readiness, quantity certification and the versioned entry-funds authority below, deadline and unknown-send checks.
3. Search the entire eligible cohort for valid activated predictions strictly above the existing 10-basis-point hurdle. Any qualifying signal takes priority over exploration, including a previously used stock when unused stocks have only weak signals. This is a numeric selection threshold, not a calibrated probability of profit.
4. Rank qualifying signals by current-session reservation count ascending, prediction descending, prior-session reservation count ascending, then configured-universe order. This favors unused stocks, then stronger signals within the same daily-use tier; historical usage only breaks equal-signal ties. Repeats remain allowed when no unused signal qualifies.
5. Only when no eligible signal qualifies, rank exploration candidates by current-session reservation count ascending, prior-session reservation count ascending, then configured-universe order. Historical counts rotate experience across days instead of restarting at the same first stocks. Preserve the selected stock's real zero/negative prediction; label the decision `EXPLORATION`, rather than inventing a positive score. COLLECT produces exploration. An invalid model/activation is an authority failure, not a weak signal.

Persist enough cohort, prediction and selection evidence for claim-time replay. A caller-provided exploration Boolean is not sufficient authority. A replay must establish that the selected member follows the rule over the captured cohort. Record unavailable members and the reason for a wait if none is eligible.

The same selection contract must be applied during claim and final send. The selected symbol, not the legacy default symbol, scopes health checks, recovery and guardian price refresh. The guardian refreshes marks for actually owned open lots even if their symbols are no longer selection candidates.

## Durable limits and recovery

The first policy caps six reservations per verified exchange session, with one concurrent core lot and existing buying-power reserve. It must not submit once per poll or duplicate an entry after a restart. An entry boundary is derived from account, owner, verified session and completed source-bar end, independent of acquisition time, selected symbol, model and executable release.

Within the existing writer transaction, reject duplicate boundaries, count all session reservations that could have reached submission (including completed trades), recheck the global slot and insert decision/protection/intent/link together. Only the existing definitive pre-send proof can release a reservation. Timeout, generic ERROR, missing outcome or an unknown submission never releases it. Recheck current membership/cap at final send without charging the current reservation twice. A conservatively consumed broker-rejected reservation is reported honestly.

Existing durable links and immutable decision evidence may supply this query; no new schema is required merely to add a counter. Keep queries bounded to the owner/session where possible. Do not rewrite old migration definitions, historical IDs or older decision payloads.

The six-entry cap is a bounded daily experience policy, not a guarantee of six fills under unavailable market/broker conditions. Independent guardian/outcomes processing continues after the cap is reached. No old position is reset or liquidated to manufacture room.

## Current funds and incomplete fee accounting

The real adapter does not provide an independent fill-net-cash ledger. Unknown fill fees therefore leave exact ledger cash incomplete; gross learning alone would still allow the first trade and block later entries. For `SIGNAL_OR_EXPLORE_V1`, add explicit `BROKER_PAPER_BALANCE_V1` authority for current funds used to size bounded PAPER entries. Preserve legacy `MODEL_ONLY_V1` exact-cash requirements.

Keep quantity certification, exact account/store/owner/config/commission binding and current ledger generation. A marked proof must link to immutable typed evidence from bounded, stable broker account/position reads with current open-order coverage. Actual current cash and buying power authorize sizing; they do not certify reconstructed historical cash. Retain `expected_cash=None`, `cash_complete=False`, unknown actual fees and null net P&L. Do not invent an independent cash activity, a balancing residual, or a RECONCILED status.

Persist source-bound evidence under the same transaction as the quantity proof. Validate its identity, source, stable capture within five seconds, age at most 120 seconds, account restrictions and current ledger at claim, execution gate and locked send, as well as health recovery/resume. Use the actual commissioned PAPER adapter; fixture authority is explicitly synthetic and cannot authorize broker PAPER. Require no competing or unexplained broker open order for a new entry. Protection and outcomes must continue without entry-funds permission.

Preserve position/basis mismatches, known cash discrepancies, disputed-source incidents, unknown sends, manual stops, guardian/time freshness, daily limits and current risk sizing: 0.5% equity, cash availability and the existing buying-power reserve. A balance receipt cannot clear an integrity incident or resurrect a revoked exact-cash certificate. A changed source, ledger generation or account balance between evaluation and submission requires fresh authority; no network request belongs inside the send transaction. Existing quantity certificates and acquisition storage suffice; no new schema is needed.

The decisive witness must execute successive entries with normal adapter-shaped account/fill responses, no independent cash-activity records, and unchanged unknown fees/net. Older strict mode must still refuse the same fee-incomplete state. This explicitly changes PAPER entry authority; it does not establish fee completeness or real-capital readiness.

## Daily learning v3

Add `daily-paper-exploration-walk-forward-v3` with target `owned-executed-paper-gross-return-v1`. The earlier v2 net-return procedure remains byte-compatible and replayable. Preserve the economic holding/exit `policy_id=collect-owned-30m-stop100-v1`; version selection separately so retaining compatible earlier outcomes does not silently discard the entire history. Selection provenance remains attached to each original decision.

The v3 learner uses exact executed gross P&L divided by entry notional. It can admit a reconciled, closed, owned outcome with `COSTS_PENDING` only when `FEE_UNAVAILABLE` is its sole remaining incompleteness and gross P&L is established. It must not change original fee fields, cost status or null net P&L. Predictions are returns before fees, not demonstrated net profitability. No contractual zero is inferred from a missing fee or from Alpaca's general statement that paper simulation excludes regulatory fees.

Both positive and negative outcomes are training examples. Retain all temporally available compatible history, excluding protected data, quarantined evidence, pending corrections, invalid ownership and other noncausal rows. For v3, overlapping feature windows within training/history do not alone discard an otherwise valid trade. Preserve strict train-versus-validation chronology, actual label availability, the 30-minute purge/embargo, and session-level validation. V2 retains its earlier overlap rule unchanged.

Warmup remains ten completed exchange sessions, including quiet sessions. Then consider one bounded learning procedure per completed session. Keep the existing five past-only expanding folds, at least 30 training examples across five sessions and ten validation examples across five sessions, fixed ridge procedure and validation gates. The latest ten completed sessions receive raw weight 2; older eligible sessions receive raw weight 1, normalized during fitting. After successful procedure validation, refit on all eligible retained history. The final all-history artifact has no independent held-out score after consuming the development validation rows.

Six usable completed trades per normal session can meet the day-ten sample floors without discarding adjacent feature windows. Short sessions, rejected/unfilled orders or invalid evidence can still leave insufficient data. In that case, record the day's learning receipt and exact reason; do not claim a fit or alter weights without data. Candidate failure or rejection retains the incumbent while exploration continues. Daily learning is always attempted when due; daily parameter changes and daily profit improvement are not guaranteed.

Carry exact target/procedure/policy through the receipt, permanent trial charge and activation authority. Use `DAILY_BOUNDED_V3` for automatically activating the new gross procedure; retain `DAILY_BOUNDED_V2` for the legacy net procedure and `REVIEW_EACH_CANDIDATE` for explicit candidate approval. Never broaden an older commission to authorize a different target. Never treat a net-return predictive incumbent as a gross-return predictor. New commissioning starts from the target-neutral COLLECT model; any existing incompatible predictive incumbent requires an explicit supported transition, never a silent reinterpretation.

## Compatibility and deployment

New optional configuration/decision fields are omitted from serialization when absent, preserving old immutable hashes. Legacy single-symbol behavior remains selectable. The new configuration is an economic selection/learning change, not a compatible executable-only maintenance transition. The Omen has not commissioned the prior core, so it can prepare the new contract for initial commissioning after the legacy handover blockers are resolved; an existing commissioned store must not simply reopen with a different config identity.

Keep the fixed PAPER endpoint and verified account, inherited owner IDs, original broker/client IDs, all back data and manual-stop history. The outstanding IWM exit remains under its original owner. Do not cancel it or override uncertain sends to force commissioning. Current staged tasks remain disabled until the successor manifest, migration/handover and independent-role readiness are verified. No LIVE credentials, paid CI, external communications or trading-state reset are part of this change.

## Required evidence

- Strong signal selected ahead of weak candidates; no strong signal produces labelled exploration with the actual prediction retained; unavailable or held members do not block eligible alternatives.
- Captured cohort selection replays at claim; forged fallback, stale features or invalid activation refuses.
- Restart, changed acquisition timestamp and concurrent claimers produce one semantic boundary entry. Six prior reservations, including completed/unknown-send cases, refuse a seventh; definitive-unsent release remains narrowly valid.
- Different stocks receive successive entries, and the selected non-default symbol receives stop/deadline protection and correctly scoped health recovery.
- V3 learns from actual positive and negative gross outcomes with null fees preserved; v2 remains strict about net completeness. Gross/net authority mismatch refuses.
- Ten-session mixed-outcome fixture exercises real bounded fitting, past-only folds, all-history final inputs and recent weights. Within-training overlap is retained; future labels, fold-crossing leakage and protected/correction-pending rows remain excluded.
- Existing final-send, protection, activation-race, restart, temporal-learning and holdout tests remain passing. Synthetic acceptance is not reported as an actual broker lifecycle.

References: [baseline design](2026-10-13-paper-core-design.md), [current handover](../../evidence/paper-core/handover.md), [Alpaca paper simulation scope](https://docs.alpaca.markets/us/v1.4.2/docs/paper-trading), [account activity fields](https://docs.alpaca.markets/us/docs/account-activities), [account funds and restrictions](https://docs.alpaca.markets/us/docs/account-plans).
