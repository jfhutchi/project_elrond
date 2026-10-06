# Stable broker snapshot acquisition

This candidate changes acquisition and diagnostics, not reconciliation tolerances,
strategy selection, risk limits, position ownership or kill-switch authority.

## Acquisition contract

Each sample brackets the positions response with two read-only account responses.
The complete account wrappers must match, including identity, restrictions and order
entry availability. Equity/cash/position marks and individual marked quantities must
pass the existing strict money boundary: a difference of one cent or more is rejected.
Duplicate or malformed positions are rejected. Only acquisition instability is retried;
a genuine difference against durable expected state is never retried until it matches.

There are at most two attempts per sample. Ledger synchronization and independent
comparison use two distinct samples, sharing a maximum five-second acquisition budget.
There are at most twelve account/position GETs, never broker mutations. A failed first
sample aborts preparation rather than substituting it for both sides of the comparison.
Timeout, provider error, reversed clock, unavailable or stale capture refuses entry.
Raw exception messages are excluded from the diagnostic journal; exception class and
HTTP status are retained. Failed and successful attempts remain in reconciliation
summary JSON, requiring no schema migration.

The recorded capture time is completion of the final accepted response, not cycle
start. This establishes local availability only. Provider quote-as-of time and atomic
cross-endpoint valuation are **not verified**. The existing verified broker-clock and
market-data gates remain necessary. An acquired sample older than 120 seconds, or
from the future relative to the checking clock, cannot authorize entry or adoption.

## Serialization and comparison

Production entry cycles prepare both samples before acquiring the existing writer lock
shared with deadline handling. No database write or order recovery occurs during
preparation. Deadline writers and the separate protection lock remain available while
the added reads wait or time out. Existing fill/order reads, acknowledgements, strict
comparison, durable writes and all trading decisions remain under writer serialization;
this change does not remove pre-existing contention from those operations.

Ledger synchronization still requires independently derived fill-ledger quantities to
match reported positions before adopting an account snapshot. The subsequent sample
is compared against frozen expected account/position values. Submission acknowledgement
may legitimately update expected orders/fills, but cannot replace those frozen values.
Cash, quantity, identity and unexplained equity differences continue to refuse entry.
Unresolved pending intent IDs remain reported even when acquisition fails.

Durable difference IDs now include reconciliation identity so repeated genuine or
acquisition failures produce separate records instead of primary-key collisions.
Existing historical records are unchanged. Repeated writes of the same reconciliation
retain the repository's existing idempotency/conflict checks.

## Release and acceptance

Both one-minute and five-minute execution bindings include the shared acquisition,
recovery, reconciliation, runner and cycle source. Updating these files therefore
requires a new exact software binding; the frozen model and economic parameters do
not change. No binding, strategy authority, task or runtime is changed by this source
candidate. No GitHub publication or hosted CI is required for the local artifact.

Deployment requires explicit approval for the reviewed source pin, software rebinding
and affected reconciliation/entry launchers, with task/config and durable-state backup
without credentials, audited rollback, and fresh post-deployment receipts. Schema 29
remains unchanged. Protection and deadline launchers need no planned restart. The
currently engaged kill switch and paused entry task remain untouched; clearing/resuming
is a separate recovery decision based on genuinely fresh readiness evidence.

Synthetic regression tests demonstrate prevention of reproduced mixed sampling and
strict refusal of real differences. They do not establish the cause of a historical
cent discrepancy or prove the deployed broker loop recovered. Prospective runtime
acceptance must verify real acquisition evidence, reconciliation, protection/deadline
liveness and unchanged limits before any approved entry resumption.
