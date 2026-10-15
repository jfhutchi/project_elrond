# What experience changes, and what still needs evidence

> **Local installation, October 15, 2026:** PR 290 is merged. Its isolated Omen release passed exact task-identity verification at 06:23 Eastern. The trader remains uncommissioned pending the original IWM exit and compatible protection/schema handover. See the [measured local installation](evidence/paper-core/local-deployment-2026-10-15/README.md).

## Current PAPER core learning contract

The [PAPER core](paper-core.md) baseline is merged in PR289. The
[approved successor](superpowers/specs/2026-10-14-daily-paper-exploration-design.md) is not
merged to main or commissioned: The successor is implemented and locally tested, including the combined trading-to-learning witness. See the [handover](evidence/paper-core/handover.md)
for integration/preparation status. The following describes the approved learning contract,
with the completed combined offline witness kept separate from deployed results.

V2 retains fixed-ridge `owned-executed-net-return-v1`. V3 adds
`daily-paper-exploration-walk-forward-v3` and `owned-executed-paper-gross-return-v1`, exact
executed gross P&L divided by entry notional. Preserve holding/exit
`collect-owned-30m-stop100-v1`, compatible history and each original decision's selection
provenance; version selection, procedure and target separately. Neither target is the
earlier market-label learner or the legacy exploration counter below.

The independent outcomes role resolves attributable entry/exit fills, terminal order
evidence, protection and reconciled flatness **for the owned lot**, while inherited holdings
keep their original owner. Broker net is populated only with complete actual cost evidence
(`BROKER_REPORTED` or applicable documented `CONTRACTUAL_ZERO`). Unknown fees remain
`COSTS_PENDING`; modeled costs are separate, and execution-price effects are not deducted
again. Corrections append outcome revisions, exclude dirty outcomes until reconciliation,
and never turn one trade into multiple training samples.

V3 may admit such a closed, owned, reconciled lot with `COSTS_PENDING` only when gross P&L
is established and `FEE_UNAVAILABLE` is its sole remaining incompleteness. The receipt does
not change fee fields, cost status or null net. Gross predictions are before fees, not net
profit evidence. V2 keeps its strict fee-complete admission. Invalid ownership, quarantine,
pending corrections and other incomplete or noncausal rows remain excluded in both.

After ten completed exchange sessions, including quiet sessions, consider learning after
every completed session. Eligible wins and losses both train; do not select by profitability.
Use all eligible compatible retained history, with the latest ten completed sessions at
raw weight 2 and older sessions at raw weight 1, normalized to mean 1 for preprocessing
and ridge loss. There is no 60-session retention cutoff. Preserve all prior outcomes and
their exclusion reasons. No executed label is invented for an untraded observation.

The procedure uses ten causal features, alpha 1 and seed 0, one charged candidate procedure
per completed session, and a 30-second bounded fitting child without broker credentials or
an operational database handle. Outcome processing has priority over fitting and keeps
working while entries wait or the market is closed.

Five expanding chronological folds use revisions actually available at each fold cutoff,
strict train/validation chronology and at least a 30-minute purge/embargo. V3 retains
overlapping feature windows within training/history; V2 keeps its earlier overlap rule.
The first fold needs at least
30 certified episodes across five earlier sessions; the five later validation sessions need
at least ten episodes. Protected ranges are excluded before materialization. Compare with
training-only constants and the exact historical decision models on the same episodes.
When fixed gates pass, refit on all current eligible history, including development
validation. The result has procedure-validation evidence, not an independent held-out
score after the refit. Protected final holdouts retain their separate registry/budget.

Insufficient or unchanged eligible evidence produces immutable `NO_CHANGE` with the parent
model retained. Failed fits, rejected candidates and awaiting-activation states retain
their own receipts. Initial model use and unattended daily activation need the exact
commissioned target authority: `DAILY_BOUNDED_V3`, legacy `DAILY_BOUNDED_V2` or explicit
`REVIEW_EACH_CANDIDATE`. Initial successor commission uses target-neutral COLLECT; a net
predictive incumbent cannot silently become a gross predictor or inherit V3 authority.
Activation is atomic at a safe decision boundary and the later
decision must acknowledge its model and learning receipt. A successful fit cannot by itself
grant entries, increase risk or establish profitability.

The approved hybrid policy prefers any eligible signal above 10 bps, otherwise records
exploration with the real weak/negative score. It rotates stock selection using durable
current/prior-session reservation counts, permits six reservations per session and one
concurrent core lot, and replays selection at claim/send. Failed fits or rejected candidates
retain the incumbent while exploration continues. Daily consideration does not guarantee
fills or parameter changes; the [current direction](intraday-paper-direction.md) gives the
exact ranking, risk and evidence boundaries.

Gross learning does not by itself authorize the next entry. The separately approved
`BROKER_PAPER_BALANCE_V1` current-funds contract must validate fresh stable broker balances
and open orders against quantity proof, current ledger and exact commissioned identity.
It leaves reconstructed cash incomplete, unknown fees unchanged and net null; it cannot
repair an integrity incident or replace exact cash in legacy mode. The combined witness uses this authority with zero independent cash activities. Fee-only learning alone would not establish successive-entry continuity; the actual adapter/coordinator witness supplies that evidence.

## Legacy experiment experience and adaptation reference

The following sections retain earlier coordinator behavior and its evidence limits. They
do not describe the replacement learner, its all-history procedure or its activation API.

## Owned-trade experience

The coordinator imports durable operational fill/ownership/protection/reconciliation
evidence. `resolve_lot` requires attributable entry and exit fills, terminal orders,
actual fees, protection and reconciled flat inventory. Missing or conflicting evidence
keeps an episode incomplete. Market labels are not substituted for broker outcomes.

`journal_exploration_feedback` derives an immutable after-fee experience receipt from
the exact registered policy and observation. Each policy/lot/source-state revision
is recorded once. Later valid evidence can create a new receipt; earlier receipts
are preserved. Later invalid evidence produces no replacement. Old idempotency keys
remain readable. Readers select at most one verified current revision per lot and
recompute ownership, timestamps, fees, returns, result and baseline differences.
Duplicate or inconsistent current receipts do not count, even when their JSON hash
is valid. Repeating a completed episode does not create another trade sample.

The scorecard reports all recorded receipts separately from current verified episodes.
An earlier as-of read recovers the earlier valid experience without restoring a database
or rewriting history. This is evidence rollback, not a strategy activation procedure.
Actual execution-price effects are already included; modeled slippage is not deducted
again. An executed-exit comparison is not a fixed-horizon forecast-error target.

**Current effect:** verified completed-lot count controls bounded round-robin exploration
selection. These receipts do not fit predictor parameters, grant orders or establish
edge. A code-bound successor policy does not automatically inherit prior-policy feedback;
software rebindings need a separately verified lineage transition.

## Predictor fitting and adjustment proposals

Existing research uses chronological eligible market labels rather than cherry-picking
profitable trades. Requests freeze training/validation data, family, generation and parent.
Protected ranges and unavailable labels are rejected before fitting. The canonical
registry permanently charges each attempt before the engine runs; failures/timeouts
consume budget, and recreating local state cannot erase search burden.

Initial constant/ridge candidates and bounded ridge successors are immutable. A successor
requires five fresh complete training sessions and five later unseen validation sessions;
reused validation dates and ambiguous parent lineage are refused. Seen validation loss
remains a diagnostic. Fresh forward assessment, costs, sample/calendar coverage and exact
economic attribution retain their own gates. The explicit result can remain `NORMAL_WAIT`
or insufficient evidence; a trade/day does not force a parameter change.

## Application and remaining gaps

An activation package is a proposal. Applying an exact fitted artifact requires the
existing package-specific approval, operational deployment/promotion, clock and binding
checks. Broad consent and synthetic demonstrations cannot activate a real model. A second
active model switch is refused by `EXPLICIT_ACTIVE_MODEL_SWITCH_PROCEDURE_REQUIRED`;
automatic challenger application and a verified active-policy feedback transition are
not completed by this feedback enhancement. Incumbent evidence and proposed rollback
identity remain retained; there is no new automatic strategy rollback authority.

Remaining research/product acceptance: genuine eligible owned episodes and daily close
comparisons, charged fresh-data successors, a preregistered unseen incumbent comparison,
an exact permitted activation/lineage transition, and beneficial later performance after
costs. Positive sample P&L or a 70–80% profitable-day aspiration does not establish any
of those steps. No automatic live transition exists.

This source change touches execution-hash-bound feedback. It is intentionally undeployed
until exact frozen binding and policy continuity are reviewed. It changes no model, risk
limit, holdout, task cadence, credential, stop, operational schema or trading authority.
