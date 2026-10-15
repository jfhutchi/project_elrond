# What experience changes, and what still needs evidence

## Current PAPER core learning contract

The [PAPER core](paper-core.md) implementation is merged in PR289; functional activation
and natural broker-PAPER acceptance remain pending in the
[deployment handover](evidence/paper-core/handover.md). It fits a fixed ridge predictor to
`owned-executed-net-return-v1`, the net return on entry notional of the exact owned lot
under its fixed stop/deadline/selection contract. This differs from earlier market-label
learners and from the legacy exploration counters below.

The independent outcomes role resolves attributable entry/exit fills, terminal order
evidence, protection and reconciled flatness **for the owned lot**, while inherited holdings
keep their original owner. Broker net is populated only with complete actual cost evidence
(`BROKER_REPORTED` or applicable documented `CONTRACTUAL_ZERO`). Unknown fees remain
`COSTS_PENDING`; modeled costs are separate, and execution-price effects are not deducted
again. Corrections append outcome revisions, exclude dirty outcomes until reconciliation,
and never turn one trade into multiple training samples.

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
nonoverlapping windows and at least a 30-minute embargo. The first fold needs at least
30 certified episodes across five earlier sessions; the five later validation sessions need
at least ten episodes. Protected ranges are excluded before materialization. Compare with
training-only constants and the exact historical decision models on the same episodes.
When fixed gates pass, refit on all current eligible history, including development
validation. The result has procedure-validation evidence, not an independent held-out
score after the refit. Protected final holdouts retain their separate registry/budget.

Insufficient or unchanged eligible evidence produces immutable `NO_CHANGE` with the parent
model retained. Failed fits, rejected candidates and awaiting-activation states retain
their own receipts. Initial model use and unattended daily activation need the exact
commissioned authority; activation is atomic at a safe decision boundary and the later
decision must acknowledge its model and learning receipt. A successful fit cannot by itself
grant entries, increase risk or establish profitability.

The newly requested daily PAPER exploration across multiple stocks is a follow-on behavior
requirement. The prepared release binds XLE and its benchmark dependencies; it does not yet
implement that requested multi-stock daily collection policy. Daily consideration is not a
claim that every day supplies broker fills or a valid parameter update. The current
[direction](intraday-paper-direction.md) separates that goal from measured deployment.

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
