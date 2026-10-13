# PAPER transient acquisition pauses

This source change distinguishes a refused acquisition from a proven ledger difference.
It does not clear historical stops or establish runtime recovery.

Only positively identified transport timeouts, the existing total read-acquisition timeout,
and HTTP 429/5xx acquisition failures can be candidates for a retryable PAPER entry pause.
HTTP transport timeout identity is preserved separately from generic connectivity errors.
A failed health GET can carry the same narrow classification; it remains unhealthy and
cannot authorize orders. Existing provider retry counts and capture bounds (two attempts,
five seconds total) are unchanged.

The actual recovery result must still refuse readiness. Pause eligibility requires the
PAPER configuration and runtime, an existing pinned local baseline, no unresolved submission,
no material reconciliation difference, and no other control failure. Partial changed or
restricted account evidence, changed positions, malformed marks, prior unstable samples,
unknown errors, clock refusals, stale captures and ingestion/ownership failures are not
converted to retryable acquisition pauses. LIVE retains its hard-failure behavior.

The existing cycle entry-pause path journals zero strategy evaluations/orders and an
unqualified day. It neither engages nor clears the global stop, and does not own independent
protection or deadlines. Manual or legacy latches remain engaged. A subsequent normal cycle,
including after a process/store reopen, obtains fresh evidence and repeats recovery, data,
strategy and submission gates. No old intent is queued or replayed by a pause receipt.

Deployment, existing-stop clearance and actual prospective recovery require separate
acceptance. Mocked HTTP and real isolated SQLite tests establish source behavior only.
