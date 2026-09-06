# Ingestion recovery and truthful completion (#198)

The existing collector reports incomplete responses as successful and reads its optimistic cursor
version after fetching. It also ignores checkpoints when planning requests. These are code defects
in the already accepted #198 contract; the existing schema and collector remain authoritative.

Use a bounded collection deadline around the existing provider retry implementation. Preserve
the current strategy warmup/correlation lookback, extending it back to an older durable checkpoint
or, for a legacy store without a checkpoint, the oldest latest observation across configured
symbols. Refuse recovery if the available calendar cannot cover that position. Record expanded
historical recovery as BACKFILL; it cannot refresh live-success timestamps. Record the requested
range before fetching. No external worker, queue, scheduler, or new database is needed.

Capture the cursor version before network I/O. Commit validated observations and a successful
checkpoint together. An incomplete universe commits useful valid observations but records FAILED
without advancing any cursor. Exact response duplicates are deduplicated; conflicting duplicates
are refused. Provider identity must match the configured query. Terminal provider errors and
timeouts remain visible and propagate. Cancellation records failure and re-raises cancellation.

Keep the existing unhealthy DataSyncResult contract for incomplete data. Resolve operational
incidents only on observed live recovery. Verify false-health prevention, replay after partial
failure, concurrent checkpoint refusal, recovery beyond the normal lookback, legacy-store
recovery, bounded provider retries/timeouts, and disabled-source behavior with temporary SQLite
stores and fake network transports. No deployed database is opened or migrated by this work.

Alternative: add a second outer retry loop. Rejected because Alpaca and calendar clients already
retry and multiplying their budgets obscures the maximum work. Alternative: replace the collector
with a new scheduling service. Deferred to #143; unnecessary for the correctness defects here.

Operational recovery/backfill on the authoritative deployment remains a separate operator action.
