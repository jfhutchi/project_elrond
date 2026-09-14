# Omen integration and deployment — 2026-09-13

What was merged, verified, recovered, deployed and measured on `LEANNES-LAPTOP` on Sunday 2026-09-13,
including the one step that needed the operator. All times UTC. Evidence files are under
`D:\Elrond\evidence\deploy-2026-09-13\` on the host (outside Git); they are named in brackets.

## Outcome

| | State |
|---|---|
| Integration | All five branches merged through PRs, plus one fix found by verification. Final `main` = **`92c49d4c5b674438ca0ebf32ccccd12c63086dd8`** |
| Verification of `92c49d4` | Full CI contract passes: lock, ruff, mypy linux + win32 (src and helpers), 2950 passed / 46 skipped / 0 failed, clean tree |
| `PROJECT_STATUS.md` deadlock on the host | **Cleared** at 12:33:44, evidence preserved; **fix deployed and regression-tested on the host** (post-deployment Phase 6) |
| Deployment of `92c49d4` | **Done.** Elevated run `run-20260913T221513Z`, operator-started 22:15:13. Tasks re-pinned 22:20:52; services started by their own triggers 22:21:51–52 |
| Live-store backup | Fresh backup 22:15:30–31 immediately before migration, verified independently |
| Migration 0028 on the live store | **Done** 22:15:32–34: schema 27 → 28, integrity ok, every existing row preserved |
| Autonomous research | First scheduled run on `92c49d4` (22:21:52) proposed `auto-benchmark-move-size-persists-v1-us-equities-daily` by itself and moved it to `SCOUTING`. The next (04:21:53) measured it under `WindowsSandbox` as `erlond` over the 293 exploratory sessions only, and graded it `UNDERPOWERED`. Nothing was seeded, rerun or tuned |
| PAPER boundary | Intact: no order, fill, position, account, reconciliation, runtime-deployment or kill-switch row changed |
| Deployment script | **Deployment succeeded; the deployment script terminated before its final verification stage.** It completed backup, migration and task re-registration; its next append to `steps.txt` failed because a separate log-watching process held that file open (below). Task Scheduler then started all six tasks through their normal triggers, and every skipped verification was measured independently |

## Why deployment needed an operator

Every Elrond application task runs as `LEANNES-LAPTOP\erlond`, logon type *Password*, and its action
pins the commit it may run (`-Commit 5025b21…`). `start-service.ps1` refuses a checkout whose HEAD
differs from that pin (`CHECKOUT_PIN_MISMATCH`), so activating `92c49d4` requires changing the pin.

The repository's supported way to change it is `deploy\omen\setup-windows.ps1 -Mode Boot -Credential
… -UpdateManaged`. The installer refuses Boot mode without elevation (`BOOT_MODE_REQUIRES_ELEVATION`)
and without the runtime account's password (`BOOT_MODE_REQUIRES_CREDENTIAL`), and a Password-logon
task cannot be re-registered without that password in any case.

`LEANNES-LAPTOP\leepa` is a member of the local Administrators group but normally runs with a
UAC-limited token, and therefore requires explicit elevation for administrative operations.
`whoami /groups` in this session shows `BUILTIN\Administrators` as *Group used for deny only*: the
membership is present and filtered out of the non-elevated token. This session ran with that limited
token and did not elevate; the operator, as the same account, launched the deployment from an
explicitly elevated window. **Correction:** an earlier version of this record said `leepa` is not an
administrator, which was wrong. The session never used the `erlond` password; the operator typed it
into the deployment script's `Get-Credential` prompt, which passed it only to the installer.

Everything else in the deployment is within `leepa`'s rights (file ACLs grant Modify on the store and
Full on the checkout; the task ACL grants `leepa` full control). It was deliberately **not** started:
stopping the services, switching the checkout or migrating the store without being able to re-pin
the tasks would leave the daemon and **protection** stopped with ten open positions, the old pinned
code unable to open a schema-28 store, and the Monday 13:30 open approaching. A blocker stops the
unsafe action, not the safe ones, so the deadlock was cleared on the running deployment instead.

## Phase 0 — evidence preserved before anything changed (12:24–12:26)

* Deployed checkout `C:\Users\leepa\elrond-omen-runtime` @ `5025b21…`, branch `codex/omen-paper-runtime`,
  one dirty path: ` M PROJECT_STATUS.md`, SHA-256 `26dabbf5…3210fa4`, mtime 2026-09-12 15:15:36.44,
  size 1324. Copied to `PROJECT_STATUS.dirty-deployed-5025b21.md` with its diff [phase0-record.txt].
* **Its writer was `doctor`.** Its "Next highest-priority task" reads *Resolve outstanding doctor
  reasons*, which only `doctor` writes, and it was written into the checkout, so `doctor` ran with the
  checkout as working directory. No scheduled component runs `doctor`; the invoker remains **UNKNOWN**.
* First `CHECKOUT_NOT_CLEAN`: ingestion 2026-09-12T15:15:36.82, research 15:15:37.29, report
  21:15:37.29. Refusals before recovery: ingestion 43, research 4, report 1 [phase0-logs.txt].
* Last successes before the deadlock: ingestion run `SUCCEEDED` 2026-09-12T14:45:38.81 (ledger
  `ingestion_runs`); research log last written 2026-09-12T09:15:37 (44 runs, `advanced: []`); report
  2026-09-11T21:15:37. **Correction:** the status-file fix's commit message said research "has never
  completed against the deployed store" — it had, 44 times, on an empty queue.
* Live store: db `36659f02…`, wal `a94be059…`, shm `4e930051…` (unchanged since the 2026-09-13T00:21
  baseline); schema 27; 0 research tasks; orders 11/10/11; kill switch disengaged since
  2026-09-10T21:15:23 [phase0-live-store.txt, phase0-store-state.txt].
* Tasks and processes: Console/Daemon/Protection/ModelServer running (process trees since
  2026-09-09/10/11), Ingestion/Research/Report `Ready` with last result 2 [phase0-runtime.txt].

## Phases 1–2 — Git and merges (12:26–12:39)

All five heads matched the previously verified SHAs; `claude/research-proposer` contained
`claude/windows-sandbox-backend` and migration 0028; its post-`ac546f3` diff was docs only. #230 was
based on `codex/market-hours-monitoring` (auto-delete is off), so it was retargeted to `main` before
merging — merged as-is it would have landed on the other branch.

| PR | Branch head | Merge commit | Merged |
|---|---|---|---|
| #229 | `c45be7e` | `83b2854` | 12:27:27 |
| #230 (retargeted to main) | `9b44885` | `5592c21` | 12:27:57 |
| #236 | `4f4c28b` | `200a45b` | 12:28:16 |
| #237 | `88d2f6a` | `0a3e5e0` | 12:28:32 |
| #238 | `068869b` | `198e10d` | 12:28:55 |
| #239 (verification fix) | `ab18004` | **`92c49d4`** | 12:39:03 |

After each merge: both parents checked, the new commits listed, and the remaining branches
test-merged with `git merge-tree` (clean). No commit reached `main` that is not in one of these
branches. `claude/trial-runtime-integration` and `claude/integration-verify-2026-09-13` were not
merged. PR #231 (`codex/console-market-status`) remains open against its own base, out of scope.

## Phase 3 — clean-main verification

The repository's CI contract (`scripts/ci_checks.py`), command by command, plus targeted groups, in
fresh worktrees whose venvs are built on `D:\Elrond\tools\python\cpython-3.11.16-windows-x86_64-none`.
Each exit status is the command's own.

**`198e10d` failed one step:** `mypy --platform linux src` — 15 errors in `sandbox/_win32.py` and
`sandbox/windows.py`. The pre-merge baseline passes it; #237 had only been checked against win32. Cause:
branching on an alias (`_WINDOWS = sys.platform == "win32"`) that mypy cannot narrow. Fixed in #239
with direct `sys.platform` branches and no runtime change. Everything else at `198e10d` passed
(2950/46/0 full suite) [verify-198e10d/].

**`92c49d4` (deploy target)** [verify-92c49d4/summary.tsv]:

| step | UTC start | s | exit | result |
|---|---|---|---|---|
| `uv lock --check` | 12:46:03 | 0 | 0 | |
| `uv sync --frozen --group dev` | 12:46:03 | 0 | 0 | |
| `ruff check src tests` | 12:46:03 | 1 | 0 | |
| `mypy --platform linux src` | 12:46:04 | 15 | 0 | no issues, 232 files |
| `mypy --platform win32 src` | 12:46:19 | 12 | 0 | no issues |
| ruff + mypy linux/win32 on CI helpers | 12:46:31 | 3 | 0 | |
| `pytest` (full) | 12:46:34 | 598 | 0 | **2950 passed, 46 skipped, 0 failed** |
| migrations: `test_migration_authority`, `test_storage_parity`, `test_storage` | 12:56:32 | 36 | 0 | 89 passed, 26 skipped (PostgreSQL not configured) |
| sandbox: `tests/unit/sandbox`, `test_sandbox_windows` | 12:57:08 | 21 | 0 | 46 passed, 1 skipped (non-Windows refusal) |
| proposer: `test_proposer`, `test_cycle`, `test_autonomous_research_cycle` | 12:57:29 | 35 | 0 | 79 passed |
| reconciliation: `-k reconcil` | 12:58:04 | 21 | 0 | 72 passed, 2 skipped |
| status file: `test_project_status_location`, `reporting/test_status` | 12:58:25 | 4 | 0 | 5 passed |
| service/CLI: `test_service`, `test_cli` | 12:58:29 | 51 | 0 | 55 passed |
| `git status --porcelain --untracked-files=all` | 12:59:20 | 0 | 0 | empty |

## Phase 4 — backup (12:33:14–12:33:16)

`quantbot backup-database --database D:\Elrond\state\quantbot-paper-fresh-20260908.db --to
D:\Elrond\backups\quantbot-paper-fresh-20260908-pre-0028-20260913T123314Z.db` (SQLite online backup,
read-only source connection, safe under a live writer). Output `ok: true`, schema 27, strategy
`adaptive-momentum-v1-309894d8d8a5296e`, 4,489,216 bytes; backup SHA-256
`648e29155147cc38e713529a35daeafa6d64af0b69c68f04fd3d6e617f6381c1`; source db/wal hashes unchanged
by the backup. The recorded `exit_code 0` in [phase4-backup.txt] was captured from an `echo`, not from
the command — the command's success is established by its own `ok: true`, which it prints only on its
return-0 path, and by the verification: `integrity_check ok`; all 55 tables' row counts equal the
Phase 0 byte copy; `restored_identically(phase0 copy, backup)` true [phase4-backup-verify.txt].

The deployment script takes a second, fresh backup after quiescing, because ingestion has written
since this one.

## Phase 6 — deadlock cleared on the running deployment (12:33:44)

Exactly one dirty path, still matching the preserved hash; restored with
`git -C C:/Users/leepa/elrond-omen-runtime restore PROJECT_STATUS.md` (exit 0). Afterwards `git status
--porcelain --untracked-files=all` is empty and HEAD is unchanged at `5025b21`. The working-tree
SHA-256 differs from the committed blob only by CRLF checkout [phase6-restore.txt].

## Phase 11 (on `5025b21`) — ingestion recovered

| scheduled run | launcher refusal | component log | ledger | checkout after |
|---|---|---|---|---|
| 12:45:35 | none | `sync-data ok`, 0 bars (weekend) | `SUCCEEDED` 12:45:37.81→39.52 | clean |
| 13:15:35 | none | `sync-data ok`, 0 bars | `SUCCEEDED` 13:15:37.69→39.54 | clean |

`Elrond Ingestion` last task result moved from 2 to 0. Zero new bars is the expected weekend result:
`completed_through` stays at the Friday close. Research next fires 15:15 and report 21:15 on the same
pinned commit; neither was observed before this record was written.

What this does and does not prove about the deadlock: the *condition* is cleared and two later
scheduled runs were accepted. The *fix* (#236, status written to the reports directory) is on
`main` but not yet running, so "status generation no longer dirties the checkout" is proven only by
the fix's regression tests until `92c49d4` is deployed. (It has since been deployed and exercised on
the host: see *Post-deployment acceptance, Phase 6*.) `5025b21` still writes the file relative to the
working directory; its scheduled services run from `D:\Elrond\state`, so only a manual `doctor` from the
checkout can re-dirty it before deployment.

## Phase 8 rehearsal — migration 0028 on a copy of the live store (12:40:41)

`migrate-database --confirm` with `92c49d4` on a byte copy: exit 0 in 0.65 s, 27→28, `research_proposals`
created with its five CHECK constraints, both UNIQUE constraints, the foreign key and
`ix_research_proposals_attempted_at`; no pre-existing table changed row count; `integrity_check ok`;
`foreign_key_check` clean [migration-rehearsal/]. The deployment script's own backup-and-migrate code
was then run verbatim against another copy and passed [script-logic-test/].

## Phases 12–13 on the exact deploy SHA (pre-deployment, against copies)

* **Containment**: `tests/integration/test_sandbox_windows.py` at `92c49d4`, 26 passed (12:59:43–59:59),
  including `test_the_memory_ceiling_refuses_the_allocation_itself`,
  `test_the_process_ceiling_refuses_a_fork_bomb_by_evading_the_static_scanner`,
  `test_descendants_do_not_outlive_a_terminated_run`,
  `test_a_descendant_dies_even_when_the_script_exits_cleanly`,
  `test_no_credential_reaches_the_child_even_when_the_scanner_is_evaded`,
  `test_the_deployed_runtime_checkout_is_unreachable`, `test_the_workspace_itself_remains_writable`
  and `test_network_is_refused_by_the_kernel_and_survives_reimporting_socket`. Real-path probe (13:00:00):
  static layer refuses the dotenv literal; under AppContainer the dotenv, live db, live wal, deployed
  checkout, repository, user profile, profile listing and a write into `D:\Elrond\state` are all
  `PermissionError`; 127.0.0.1:11434 and :8787 time out; broker DNS fails; child environment has no
  `ALPACA_*`/`QUANTBOT_*`; workspace write OK [containment-92c49d4/].
* **Research wiring**: `start-service.ps1 -Component research` from a clean `92c49d4` worktree against
  a migrated copy (13:00:41–13:01:04, exit 0): stages `PROPOSED,SCOUTING` (Windows sandbox selected),
  `qwen3:4b-instruct` (digest `0edcdef34593…`) reached and chose, proposal `CREATED`, task moved
  PROPOSED→SCOUTING, `code_commit` `92c49d4…`. Then two `research-propose --commit` attempts on unchanged
  state: `NO_ELIGIBLE` recorded once, the second wrote nothing (same `prop-e59ec91f3dc93a9e7455780b`)
  [wiring-92c49d4/]. The model's response hash equals the 2026-09-12 proof's, which is expected: the
  store data and slate were identical, so the prompt was byte-identical, and the same model digest
  decodes deterministically from a fresh process.

These are measurements of the exact code to be deployed, made before it was deployed. The deployed
runtime's own measurements are under *Post-deployment acceptance*.

## Phase 14 — PAPER boundary on the running deployment (13:16)

Byte copy after the two recovered ingestion runs versus the Phase 0 copy: the **only** changed table
is `ingestion_runs` (306→308, both `SUCCEEDED`). `order_intents` 11 (latest 2026-09-09), `broker_orders`
10, `fills` 11 (latest 2026-09-10), `positions`, `reconciliation_runs` (latest
`run-20260911T200500Z RECONCILED`), `account_snapshots` and the kill switch (disengaged,
2026-09-10T21:15:23) are unchanged. Account identity: SHA-256 prefix of `EXPECTED_ACCOUNT_ID` equals
the latest snapshot's (`e09a79ca34d782e3`). Dotenv unchanged since 2026-09-08: `TRADING_MODE=PAPER`,
`BROKER_ENVIRONMENT=PAPER`, `LIVE_TRADING_ACKNOWLEDGED=false`, `KILL_SWITCH=false`, no `ALPACA_LIVE_*`
[phase14-safety-current-deployment.txt].

## The deployment script

`D:\Elrond\evidence\deploy-2026-09-13\deploy-main.ps1` (SHA-256 `6d5d7c55…98bbcc`, unchanged since it
was written; its probe `db_probe.py` `4593c00e…6138a`). It refuses unless `origin/main` is still exactly
`92c49d4`, the deployed checkout is clean and the PAPER settings are intact; disables and stops the six
application tasks (the model server keeps running) and waits until no service process is alive and
the WAL is quiet; takes and verifies a fresh backup; switches the checkout to `92c49d4` (the old branch
stays at `5025b21` for rollback); migrates and verifies; prompts for the `erlond` password and runs the
repository installer with `-Mode Boot -UpdateManaged`; checks the pins; starts the tasks; records
processes and logs. Its read-only preflight was rehearsed as `leepa` at 12:49:32 [run-20260913T124932Z-rehearsal/].

An operator claim at ~14:50 that it had been run was checked and found false: no run directory,
backup, PowerShell launch event or pin change existed [postdeploy-verification-20260913T1451Z.txt].

## The deployment run — `run-20260913T221513Z`

Two earlier launches from a non-elevated window refused at their first check and changed nothing:
`run-20260913T221435Z` and `run-20260913T221500Z`, both `REFUSED ELEVATION_REQUIRED` within 30 ms. The
elevated run's own records [run-20260913T221513Z/steps.txt, transcript.txt, backup.log, migrate.log,
database-before.json, database-after.json]:

| step | UTC | result |
|---|---|---|
| preflight | 22:15:13.569 | `elevated=True`; `origin/main=92c49d4`; checkout `5025b21`, no dirty path; `TRADING_MODE=PAPER BROKER_ENVIRONMENT=PAPER LIVE_TRADING_ACKNOWLEDGED=false`; schema 27, integrity ok |
| processes before (elevated, from command lines) | 22:15:1x | console host 15968 → python 24084/22348 (since 2026-09-10 21:14:39); daemon host 20052 → python 17216/432; protection host 5296 → python 12420/2556 (since 2026-09-11 17:05:35); all `--commit 5025b21…`; one tree per component |
| disable, stop | 22:15:16.515–.735 | six tasks disabled, then stop requested |
| quiesce | 22:15:20.534 | no application task running, no service process alive; WAL mtime stable over 10 s (last write 21:45:39, ingestion) |
| backup | 22:15:30.538–31.889 | `backup-database` exit 0, `ok: true`, 4,493,312 bytes → `D:\Elrond\backups\quantbot-paper-fresh-20260908-pre-0028-deploy-20260913T221513Z.db`, SHA-256 `d12adb872e4a6cdfa95de5e410a6894ad8e92a7254b2f925ec98c2764281ebb2`, integrity ok, all 55 tables' row counts equal the live store |
| checkout | 22:15:32.197 | `git switch --detach` exit 0, HEAD `92c49d4`, no dirty path |
| migration | 22:15:32.202–34.182 | `migrate-database --confirm` exit 0, `migrated: true`; schema 27 → 28, alembic 0027 → 0028, integrity ok, every pre-existing table's row count unchanged, all eight named constraints and `ix_research_proposals_attempted_at` present |
| installer | 22:15:34.363 → ~22:20:52 | credential prompt answered by the operator; `Registered password-backed startup tasks at Limited run level.`; all six tasks `Ready`, enabled, pinned `92c49d4` |
| **script terminated before final verification** | ~22:20:52 | `Add-Content` on `steps.txt` failed with *file is being used by another process* while recording the task list; under `ErrorActionPreference=Stop` the script ended there. The deployment itself had already succeeded through task registration |

**Deployment succeeded; the deployment script terminated before its final verification stage.** This
was an operational logging failure, not a failed deployment. A separate log-watching process — the
observing session's monitor, which polled `steps.txt` every 2 s from Git Bash (`wc -l` and `tail`) to
report progress — held the live step log open, and the script's next append failed. Those Git Bash
opens and PowerShell's `Add-Content` are not share-compatible. Reproduced afterwards in a scratch
directory with the same polling: 1,114 of 3,000 appends failed, and the polling reads failed too. The
script was correct; the observer broke its logging. Lesson recorded: never poll a file a running script
appends to.

**What did not run, and what replaced it.** The pin check, `Start-ScheduledTask` for the six tasks,
the post-start process and launcher-log records, the final HEAD record and `Stop-Transcript`. The
installer registers each task with a repetition trigger that first fires one minute after
registration, so Task Scheduler started all six at **22:21:51–52** through their normal supervised
action. Nobody started a process by hand. Every skipped check is measured independently below. The
side effect is that the schedule's phase moved: ingestion now fires at :21:51 and :51:51, research at
04:21:52 / 10:21:52 / 16:21:52 / 22:21:52, report daily at 22:21:52 (previously 21:15:34).

## Post-deployment acceptance

All measured from the host by the session as `leepa` (non-elevated); the live store was never opened
— every store fact is from a byte copy taken with db/wal/shm size and mtime identical before and
after the copy [acceptance/].

### Git
`git ls-remote` `refs/heads/main` = `92c49d4c5b674438ca0ebf32ccccd12c63086dd8`. Deployed checkout HEAD
`92c49d4…`, detached, `git status --porcelain --untracked-files=all` empty; tree `91b75e0b…` identical
to the verified `D:\GitHub\_elrond_wt\main-92c49d4` worktree. Re-measured clean at 22:25, 22:26, 22:29
and 22:53.

### Scheduled tasks (22:22:42)
All six application tasks: principal `erlond`, logon `Password`, run level `Limited`; action
`D:\Elrond\state\service-host\1188bba2cb38de299aad0768\elrond-service-host.exe` with
`-Repository C:\Users\leepa\elrond-omen-runtime -Commit 92c49d4c5b674438ca0ebf32ccccd12c63086dd8`;
description `commit=92c49d4…`; triggers AtStartup plus repetition (5 min console/daemon/protection,
30 min ingestion, 6 h research, 1 day report). `ElrondModelServer` untouched (no pin; running since
2026-09-09). The stored password is not readable and was not read.

### Processes and commit identity
Three new service trees, each `elrond-service-host` → `powershell` → `python` → `python`: hosts 10612,
11060, 17288 created 22:21:51, interpreters 22:21:52. No other application service host exists (the
remaining host, 2056, is the model server). The old trees were proven gone by the elevated quiesce at
22:15:20. Console, daemon and protection each hold their single-instance lock file (byte-range locked
when probed at 22:28). The console answers `/api/state` with a runtime measurement taken at 22:21:54.66.

Commit identity is not inferred from PIDs. A non-elevated session cannot read another account's
command lines, and the script's elevated `process_after` record did not run. The chain that does
establish it is mechanical: each task's action pins `92c49d4`; `quantbot.operations.service` refuses
`CHECKOUT_PIN_MISMATCH` or `CHECKOUT_NOT_CLEAN` before running any component; the checkout is
`92c49d4` and clean; and no launcher log has been written since the deployment, so no refusal
occurred. The research run independently recorded `code_commit 92c49d4…` from the running package's
own Git state. **Not measured:** the elevated command lines of the three long-running processes.

### Store (copy 22:23:09)
Schema 28, alembic 0028, `integrity_check ok`, `foreign_key_check` 0 rows. `research_proposals`: 12
columns; primary key `proposal_id`; CHECKs `outcome IN (CREATED, NO_ELIGIBLE, REFUSED)`, *only a created
proposal names a task*, *a dataset sequence belongs to a task*, *a created proposal names its template
and dataset*, *dataset sequence ≥ 1*; `UNIQUE (task_id)`; `UNIQUE (dataset_identity, dataset_sequence)`;
foreign key `task_id → research_tasks.task_id`; index `ix_research_proposals_attempted_at`.

The deployment backup, verified independently: SHA-256 recomputed equal to the script's; a copy of it
has integrity ok, schema 27, 55 tables. Against the session's 16:38 pre-deployment snapshot it differs
only by eleven `ingestion_runs` (16:45–21:45) and the ingestion cursor — it is the pre-migration store.

### Phase 6 — status generation no longer dirties the checkout
The deployed checkout's code, the deployment's own environment loader and dotenv, working directory
set **to the checkout** (the 2026-09-12 condition), store pointed at a byte copy: `doctor` at 22:25:21
and again at 22:25:44. Both exit 2 with `MARKET_DATA_STALE` (Sunday; data completes Friday 20:00) —
a readiness result, not a failure of the property under test. Both wrote
`D:\Elrond\reports\PROJECT_STATUS.md` (22:25:24, 22:25:45). The tracked checkout `PROJECT_STATUS.md`
kept its mtime (12:33:45) and hash; no file anywhere under the checkout, ignored files included, was
written after 22:25:21; `git status` empty after each. The next scheduled service run (ingestion
22:51:52) started without refusal and left the checkout clean [acceptance/phase6-doctor-regression.txt].

### Phase 7 — ingestion on `92c49d4`
| run | task result | ledger | component log | checkout |
|---|---|---|---|---|
| 22:21:51 | 0 | `SUCCEEDED` 22:21:53.79 → 56.47, 6969 seen, 0 inserted | `sync-data ok`, 0 bars, `completed_through 2026-09-11T20:00Z` | clean |
| 22:51:52 | 0 | `SUCCEEDED` 22:51:54.24 → 55.96, 6969 seen, 0 inserted | same | clean |

Zero bars is correct on a Sunday. No `CHECKOUT_NOT_CLEAN`, no schema error, no launcher-log refusal. One
ledger row per trigger; concurrent writers are excluded by the task's `IgnoreNew` setting and the
component lock (`EXIT_DUPLICATE`), and no duplicate was recorded.

### Phase 15 — report on `92c49d4`
The report task's first scheduled trigger, 22:21:52: task result 0; `report-weekly ok`,
`observed_days 0`, wrote `D:\Elrond\reports\weekly\2026-36.md`; schema 28 accepted; no refusal;
checkout clean. Next scheduled run 2026-09-14 22:21:52.

### Phase 8–9 — autonomous research on the live store
The research task runs `research-cycle --max-steps 1 --commit --propose-when-idle`
(`COMPONENT_COMMANDS` at `92c49d4`). Its first scheduled run, 22:21:52, task result 0:

* Queue empty → proposer invoked; one model call, 19.3 s (model server log).
* Proposal `prop-5eb912d09df187a7f551d619`, `CREATED` 22:21:54.98, `code_commit 92c49d4…`,
  proposer version 2, selector `model`, model `openai-compatible:qwen3:4b-instruct@configured`,
  response SHA-256 `c55e115b…`, prompt `research-candidate-selection@1` (`df9d98eb…`), catalogue
  `0e0af9d8…`.
* Slate: one eligible — `benchmark-move-size-persists` v1 on `us-equities-daily` (14 clean confirmatory
  sessions from 2026-08-24, `refuted-016` adjudicated); three rejected —
  `breadth-persists-next-session`, `dispersion-precedes-volatility`,
  `dispersion-predicts-benchmark-direction`, each because all 14 unsearched sessions were already seen
  by that question (exposed through 2026-09-11).
* Task `auto-benchmark-move-size-persists-v1-us-equities-daily`, dataset sequence 1. Events:
  `→ PROPOSED` (actor `proposer/2`, 22:21:54.98); `PROPOSED → SCOUTING` (actor `research-cycle`,
  22:22:14.54, *no stored record settles this question*).
* Nothing was seeded, and no command other than the scheduled task touched the live research tables.

The outcome is the one the measured state predicts: exactly one question had clean sessions it had not
seen. The proposal id equals the one a separate process derived at 13:00 on a copy of the pre-deployment
store: same state, code, model digest and prompt, decoding at temperature 0, and the attempt's date is
excluded from its identity.

### Phase 10 — holdout boundary (bookkeeping only)
The proposal fixes `exploratory_through = 2026-08-21`, equal to `dataset_consumed_through`, which comes
from `PRIOR_WINDOW_CONSUMPTION` (`sip-us-equities-daily` 2016-01-04..2026-08-21, 68 trials; the store has
no `hypothesis_data_windows` or `search_runs` rows). `first_clean_session = 2026-08-24`, 14 clean
sessions. So the SCOUTING measurement may read sessions through 2026-08-21 and none of the 14.

What the measurement recorded (store copy 2026-09-14 04:24:30): `search_runs` row
`auto-exploratory:auto-benchmark-move-size-persists-v1-us-equities-daily:e2`, actor `autoexperiment` v2,
dataset `sip-us-equities-daily`, `data_role DISCOVERY`, `start_date 2025-06-24`, `end_date 2026-08-21`,
one candidate; its detail and the task event agree: `sessions 293`, `first_session 2025-06-24`,
`last_session 2026-08-21`, `exploratory_through 2026-08-21`, `first_clean_session 2026-08-24`, panel
SHA-256 `60bfd916…`. The store's own session calendar — dates only, from `bars.timestamp`, no price
or volume column read — holds exactly **293** sessions on or before 2026-08-21 (2025-06-24 first) and
**14** after it (2026-08-24 … 2026-09-11). The panel's 293 sessions are therefore all of the exploratory
sessions and none of the clean ones; one more session of either kind would have changed the count or
the last date. The code also refuses before measuring if a panel reaches the first clean session
(`autoexperiment.py`, `ExperimentRefused`). The ledger now holds one consumed search on this dataset
(cumulative trials 69); no `hypothesis_data_windows` row was created, because nothing was registered
[acceptance/phase10-holdout-boundary.txt, db-accept-research-0421.txt]. The clean confirmatory sessions
were not opened to verify this.

### Phase 11 — sandboxed measurement (research 2026-09-14 04:21:53, task result 0)
The scheduled research run found the `SCOUTING` task actionable and advanced it one step (the proposer
was not called: something was actionable). `stages_available` `PROPOSED, SCOUTING` — the stage is only
offered when `select_isolated_backend` succeeds, and it did under `erlond`.

| | |
|---|---|
| backend | `WindowsSandbox` (recorded `sandbox_backend`); the class declares `os_enforced = True`, and `run_measurement` refuses any backend that does not |
| policy | 256 MB memory, 90 s wall clock, network disabled |
| child runtime | 0.282 s; peak memory 7.32 MB (Job Object accounting) |
| measurement wall time | 42.0 s (`search_runs` 04:21:56.119 → 04:22:38.131). That covers backend construction, interpreter staging and the 0.282 s child. First-time staging of the interpreter into `erlond`'s profile is the likely cost, from the code; this session cannot read that profile, so it is inferred, not measured |
| script | SHA-256 `9c317a09…41f6c3b2` (also the search's `configuration_hash`); stderr empty |
| observations | 291 lead-lag observations from 293 sessions, 23 symbols |
| statistic | `benchmark_absolute_move_persistence` = 0.02593945802823299, t = 0.4411192161936907 |
| verdict | **`UNDERPOWERED`** (04:21:56.06): confirming the pre-registered effect 0.20 against a luck bar of t = 2.910019 at 69 cumulative trials needs 210 observations, and 12 clean confirmatory observations exist (minimum detectable effect 0.748707). The exploratory statistic is recorded as exploratory evidence only; it did not decide the verdict and is not a refutation |
| memory written | `research_records` `exploratory:…:e2` (`EXPLORATORY_ONLY`) and `underpowered:…:e2` (`UNDERPOWERED`), two `research_relationships`, one `search_runs` row |

The statistic matches the 2026-09-12 proof's independently checked value on the same 293-session panel
(IC 0.026). It was not rerun or tuned. The weak result and the `UNDERPOWERED` verdict are the honest
outcome the design predicted for this dataset: 14 new sessions cannot confirm a 0.20 effect at this
multiple-testing burden.

### Phase 12 — proposer idempotency
On a byte copy of the production store as it stood after the proposal (22:29), a new process running
the deployed code called `research-propose --commit` twice. Attempt 1: `NO_ELIGIBLE`,
`prop-e59ec91f3dc93a9e7455780b`, recorded; attempt 2: the same id, `recorded: false`, no row
written anywhere. That id equals the one the 13:01 rehearsal derived on its own copy. On the same copy,
inside a rolled-back transaction, the database refused a second `CREATED` row for the same task
(`UNIQUE constraint failed: research_proposals.task_id`) and one for the same dataset sequence under a
different task (`UNIQUE … dataset_identity, dataset_sequence`) [acceptance/phase12-idempotency-copy.txt].
**PENDING (research 10:21:52 and 16:21:52):** the same property on the live store through the
scheduled task.

### Phase 13 — containment against the deployed code
Deployed checkout's `src` and venv, 22:26:20: `select_isolated_backend` → `WindowsSandbox`,
`os_enforced true`. Static layer refuses the dotenv literal. Under the kernel, with every path built at
run time: deployment dotenv, live db, live WAL, deployed checkout, this repository, a store copy and the
user-profile `.gitconfig` all `PermissionError`; listing the profile `PermissionError`; writing into
`D:\Elrond\state` `PermissionError` (no file created); 127.0.0.1:8787 (console) and :11434 (Ollama)
time out; broker DNS `gaierror`; no `ALPACA_*` or `QUANTBOT_*` in the child environment; workspace write
OK. Parent-side controls: the parent can read every target, connect to both ports and resolve the broker.
Suite with the deployed `src` on `PYTHONPATH` from the identical tree: `test_sandbox_windows.py` and
`tests/unit/sandbox` — 36 passed, 1 skipped (non-Windows refusal), including the memory ceiling, the
process ceiling, both descendant-cleanup tests and workspace destruction. Both checkouts stayed clean.
These ran as `leepa`; production runs the sandbox as `erlond` (Phase 11).

### Phase 14 — PAPER safety
Dotenv unchanged: `TRADING_MODE=PAPER`, `BROKER_ENVIRONMENT=PAPER`, `LIVE_TRADING_ACKNOWLEDGED=false`,
`KILL_SWITCH=false`, only `ALPACA_PAPER_*` credential keys. The console reports `trading_mode PAPER`,
`broker_environment PAPER`, both `ALPACA_LIVE_*` settings null, kill switch disengaged (since
2026-09-10 21:15:23), reconciliation clean with 0 diffs, 10 open positions all protected. Account
identity: one distinct account, SHA-256 prefix `e09a79ca34d782e3` (expected). `order_intents` 11 (latest
2026-09-09), `broker_orders` 10, `fills` 11 (latest 2026-09-10), `positions` 1505 — unchanged; no order
on the Sunday. The research component removes every broker secret from its environment before running
(`operations/service.py`), and generated code sees neither secrets nor the store (Phase 13); the
research service process itself does open the store, because the research tables live there.

### Phase 16 — ledger comparison
Per-table row count and a SHA-256 over every row, pre-deployment copy (16:38) versus post-deployment
copy (22:23). 49 tables are byte-for-byte identical in content, including `order_intents`,
`broker_orders`, `fills`, `positions`, `account_snapshots`, `equity_snapshots`, `kill_switch_state`,
`reconciliation_runs`, `reconciliation_diffs`, `runtime_deployments`, `position_protection_*`,
`incidents`, `research_records`, `runs` and `strategy_deployments`. Changed:

| table | change | explanation |
|---|---|---|
| `schema_version`, `alembic_version` | 27 → 28, 0027 → 0028 | migration |
| `research_proposals` | added, 1 row | migration + first autonomous proposal |
| `research_tasks` | 0 → 1 | the proposed task |
| `research_task_events` | 0 → 2 | PROPOSED, PROPOSED → SCOUTING |
| `ingestion_runs` | 314 → 326 | eleven scheduled runs on `5025b21` (16:45–21:45) and one on `92c49d4` (22:21) |
| `ingestion_cursors` | content only | cursor timestamps advanced by those runs |

Second comparison, post-deployment copy (22:23) versus the copy after the first measurement
(2026-09-14 04:24:30). Integrity ok, foreign keys clean, schema 28. Changed:

| table | change | explanation |
|---|---|---|
| `ingestion_runs` | 326 → 338 | twelve scheduled runs, 22:51 through 04:21 |
| `ingestion_cursors` | content only | advanced by those runs |
| `research_tasks` | content only | `SCOUTING` → `UNDERPOWERED` |
| `research_task_events` | 2 → 3 | `SCOUTING → UNDERPOWERED` |
| `search_runs` | 0 → 1 | the exploratory search, charged once |
| `research_records` | 25 → 27 | `EXPLORATORY_ONLY` and `UNDERPOWERED` findings |
| `research_relationships` | 0 → 2 | links for those findings |

Every trading table — orders, intents, fills, positions, account and equity snapshots, kill switch,
reconciliations, runtime deployments, protection cycles and evaluations — is identical in content.

`runtime_deployments` has no `92c49d4` row yet. Every existing row is a `COORDINATOR` observation at
20:05 on a trading day, so the first one on `92c49d4` cannot occur before Monday 2026-09-14 20:05.

## Follow-ups, not part of this objective

1. `tests/integration/test_generated_experiment.py` is WSL-only and never runs on the Omen.
2. `research-status` lists empty `AVAILABLE` datasets as clean confirmatory capacity.
3. Deployment evidence scripts live outside Git; if a repeatable deployment runner is wanted, it
   should be reviewed into `deploy/omen/` rather than copied from here.
