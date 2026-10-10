# What experience changes, and what still needs evidence

This describes source behavior, not deployed acceptance or demonstrated profitability.

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
