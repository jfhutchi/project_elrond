# Bounded learner and observer rollout preparation

This follow-on adds source-only deployment procedures to the integrated learner/alert candidate.
No task registration, code rollout, credential/principal change or chat delivery has occurred.
The scripts default to plan-only; `-Apply` requires the operator's explicit rollout authority.

`start-monitor.ps1` uses the existing windowless service host and shared kill-on-close Windows
Job ownership. Its pinned Python launcher refuses dirty/wrong checkouts, relative/missing
inputs, LIVE mode and live acknowledgement. `--check` performs no broker call, store open or
monitor write. Actual checks use a bounded existing PAPER GET clock/calendar probe and write
only `<learner>/monitor`, reading the operational database without migration.

`schedule-monitor.ps1` installs only the root `Elrond Observer` task: every two minutes and at
the existing learner principal's sign-in, with a one-minute limit and IgnoreNew concurrency.
It reuses the managed learner task's Interactive/Limited principal and exact service-host
executable. An existing observer is refused. Rollback targets only the matching owned task,
code pin and action, refuses a running observer, and retains alert/log history. No principal,
password, boot mode, firewall or remote access is provisioned.

`update-learner-task.ps1` targets only the root managed `Elrond Learner` task. It requires the
expected old code pin, matching deployment receipt, unchanged environment-file path, a new
backup directory and a validated clean candidate. It exports the original definition and saves
plain action/deployment rollback data before temporarily disabling scheduling. It never stops
an active tick, starts a worker directly, restores a database or changes model/execution authority.
It updates the action plus `deployment.json` pin and restores the original enabled state.
Existing principal, triggers, settings and description are preserved; the description's historical
commit text is not authoritative. Use action/receipt pins to verify the deployed revision.

The procedure compares the full exported scheduler definition, excluding only intentional action,
enabled-state and registration-date changes. Running/drift checks occur before modification.
Concurrent definition drift is preserved and left disabled with an explicit refusal; do not
silently resume it. A failed apply restores only its own action/metadata changes; failed recovery
leaves scheduling disabled. Rollback also refuses changed definitions/actions/deployment evidence.
Rollback never rewinds new observations, model attempts, outcomes or audit history.

Review the exact approved values for `Repository`, `Directory`, `Commit`, `ExpectedOldCommit`,
`EnvironmentFile`, `ServiceHost`, `OperationsDatabase` and `BackupDirectory`. Run the plan first,
then add `-Apply` only after approval. For rollback, use the same values and add `-Rollback -Apply`.
Keep rollback artifacts private. The operator must separately take a consistent learner-store
backup and verify the staged interpreter is visible to Task Scheduler before applying.

This option uses the existing schema-29 learner store and reads the schema-28 operational store;
it does not update the console/daemon or authorize an operational-store migration. PAPER entry,
model promotion, missing execution adapter, protected features and insufficient research evidence
remain separately gated. Fresh receipts are not proof of productive forecasts or profitability.
External delivery remains NOT_CONFIGURED until an approved route passes its confirmed canary.

Verification uses scheduler doubles for plan/refusal/apply/rollback/races/failure paths, a real
PowerShell wrapper against a harmless Python fixture, and the existing monitor/service group.
These prove code behavior, not live Task Scheduler registration or reboot/sign-out recovery.
The integrated candidate's earlier full-suite count must not be attributed to this follow-on.
