# Independent receipt monitoring

This follow-on to #251/#252/#253 runs independently of the learner. It has no broker,
application-store migration, order, model-promotion or scheduler-installation dependency.
It reads the configured worker receipt and existing operations SQLite database in read-only
WAL-aware transactions, then writes a dedicated monitor alert store and atomic JSON receipt.

The worker must have a completed, aware interval, a run ID, PAPER-disabled authority fields,
and the expected deployment code pin. Incomplete, malformed, future or mismatched receipts
cannot recover an alert. Latest protection/reconciliation failures override older successes.
During a normal STARTED tick, the monitor ages its last validated completion at the same code
pin; it does not flap to critical while that completion is fresh. STARTED cannot recover an
existing alert, and an overdue or unknown completion still raises a critical condition.
Only LIVE ingestion runs count for collection freshness; historical backfill cannot recover it.
This checks completion evidence, not continuous process liveness or future position protection.

Default maximum ages are 15 minutes for learner receipts, 20 minutes for protection, one hour
for reconciliation and 45 minutes for live ingestion. The protection loop runs every 15 minutes;
live ingestion is commonly scheduled every 30 minutes, so the defaults allow completion delay.
Thresholds are reviewable monitor policy,
not risk controls. Invalid/missing measurements are critical; overdue live ingestion is warning.
These component thresholds do not yet use an authenticated market-session clock to suppress
expected protection waits outside market hours; session-aware scheduling acceptance remains
required before commissioning this monitor. No missing clock is inferred to be a closed session.
Blocked worker ticks establish completed checks but never productive learning. Freshness alone
cannot prove forecast progress, paper fills, quote freshness or profitability.

```powershell
python -m quantbot.operations.monitor --learner-directory <learner> --operations-database <existing-paper-db> --output-directory <learner>/monitor
```

Run this from the matching reviewed code. Output must be separate from the source database.
No operational daemon is invoked. Install an independent, bounded recurring launcher only
with exact-pin rollout approval; running a learner-owned watchdog cannot detect its own death.
The console marks the monitor receipt stale after five minutes and refuses malformed evidence.
A monitor that itself stops is visible on the console, but automatic dispatch is not commissioned.

Active critical alerts retain the existing immediate/reminder policy, even after acknowledgement.
Noncritical alerts enter a local digest at most daily by default, including acknowledged alerts.
Acknowledgement is available through local same-origin console POST forms. It writes only an
existing dedicated alert store, never resolves a gate and never grants trading authority.
The redirected page immediately confirms persistence; receipt projections refresh next check.

## External delivery boundary

The existing Elrond chat is an approved destination, but the current tool catalog and repository
provide no machine-to-this-chat send interface. Chat consent does not create an endpoint or
credentials. Console, stdout, local outbox and GitHub comments are not verified chat delivery.

Minimum secure setup is a supported authenticated bridge or connector that can read a bounded,
sanitized monitor outbox and deliver to an explicitly selected destination. It needs delivery
IDs, retries/deduplication, an acknowledgement receipt and a canary the operator confirms seeing.
Host-local artifacts require an authorized reachable reader; no endpoint is guessed and no
credentials or remote access are provisioned here. Hosted scheduled chat tasks alone cannot
read this machine's local files. External delivery remains NOT_CONFIGURED until tested.

## Verification scope

The final affected monitor, alert lifecycle and complete console test group passed 191 tests.
Ruff, diff checks and both-platform source mypy checks passed. Review identified and regression
tests reproduced false recovery from incomplete receipts/newer failures and startup flapping;
the corrected cases pass. Native runtime services, scheduler installation and chat delivery
were not changed. This follow-on head has not received full aggregate pytest coverage; the
first alert batch's aggregate evidence must not be attributed to this additional code.
