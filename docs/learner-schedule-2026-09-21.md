# Learner outcome schedule, 2026-09-21

The user requested a run every ten minutes and trades for learning. The native
`Elrond Learner` task now checks historical outcomes every ten minutes. **Learner
trades and new-market forecasts are not enabled:** the forward-data producer is
missing and the learner has not qualified for PAPER execution. Scheduling does
not bypass the holdout, trial, promotion, risk or provenance controls.

## Deployment

- Development continued on `codex/elrond-learner`; fetch found 18 ahead / 0 behind
  origin/main before the scheduling commits. No merge, rebase or reset.
- Learner checkout: `C:/Users/leepa/elrond-learner-runtime`, clean detached
  `2bedc51a56ce7947714be3128debef54258c4646`.
- Console checkout: `C:/Users/leepa/elrond-console-runtime`, clean detached
  `4c4188774dc1d400a0289769590a002af71cc38d`.
- Trading checkout remains clean at `a49203d5537d2cae7647ad110d69016a23265371`.
  Daemon and protection task executable, arguments and working directory matched
  their saved pre-change values. Both tasks remained running.
- Dedicated learner database: `D:/Elrond/learner/state/learner.db`.
  Counts preserved: 1 dataset, 3 models, 3,696 predictions, 3,696 outcomes,
  10 search records (3 learner fit attempts).
- Task uses the existing native Windows service host, hidden PowerShell and the
  learner checkout's native venv. No credential file or broker client is loaded.
- `deploy/omen/schedule-learner.ps1` creates the task: indefinite `PT10M`
  repetition, logon trigger, `IgnoreNew`, five-minute execution limit, limited
  interactive principal `leepa`. User must be logged in and laptop awake.
  Password-backed boot execution and sleep/logout behavior were not tested.

The worker uses a dedicated writer lock. It resolves eligible historical outcomes
through existing guards and evaluates newly resolved evidence. It reconciles
exact prediction sets with hash-checked persisted evaluation receipts, so an
interruption between resolution and evaluation is recoverable. Unchanged evidence
does not trigger another evaluation, fit, forecast or full historical input audit.
New evaluations retain full service validation. Errors expose fixed codes only.

`D:/Elrond/learner/worker.json` is atomically replaced for each check; completed
receipts are appended to `logs/worker-runs.jsonl`. The dashboard at
`http://127.0.0.1:8787/learner` shows the last completion and resolved count.
Matching commit and fresh receipt are required; completion older than 15 minutes
is stale. A successful blocked check has task exit 0 and dashboard
`SCHEDULED_BLOCKED`, never a claim of active trading or profitability.

## Verification and recovery

The final installed worker completed at `2026-09-21T16:24:04.506501+00:00`, exit 0,
in 0.258 seconds, with zero new outcomes or evaluations. The task's next scheduled
run was 12:29:34 Eastern. Manual Task Scheduler starts verified execution; the
ten-minute timer itself had not yet fired at this checkpoint. Browser reload and
API both showed `SCHEDULED_BLOCKED` and the same receipt.

Development learner/console suite: 275 passes before the two additional worker
regressions. Final worker suite: 6 passes; console runtime suite: 174 passes.
Ruff and Windows mypy passed; Linux-platform mypy passed before the final receipt
optimization. Code review verified recovery, idempotence and receipt integrity.

First worker version completed successfully but spent 158 seconds re-auditing
unchanged historical evaluation inputs; the final version removes that redundant
audit from queue reconciliation. No evidence was refitted or reclassified.

Console restart initially returned service-host exit 70 with the prior launcher
log. Changing only its log destination to
`D:/Elrond/learner/logs/console-worker-launcher.log` restored the scheduled console.
The old log failure's underlying cause is unconfirmed; no ACLs were changed.
The learner successfully reused its own launcher log on a subsequent invocation.

Backups include `D:/Elrond/backups/pre-learner-schedule-20260921T161846Z.db`, the
timestamped pre-schedule learner deployment manifest, and
`console-before-learner-worker.xml`. To suspend this new worker without affecting
trading, disable and stop only `Elrond Learner`. Preserve its database, history and
protected evidence. Any code rollback must pair the task pin with the matching
clean runtime checkout; do not reset the development branch or trading checkout.
