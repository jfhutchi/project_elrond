# Omen integration and deployment — 2026-09-13

What was merged, verified, recovered and measured on `LEANNES-LAPTOP` on Sunday 2026-09-13, and the
one step that could not be taken by software. All times UTC. Evidence files are under
`D:\Elrond\evidence\deploy-2026-09-13\` on the host (outside Git); they are named in brackets.

## Outcome

| | State |
|---|---|
| Integration | All five branches merged through PRs, plus one fix found by verification. Final `main` = **`92c49d4c5b674438ca0ebf32ccccd12c63086dd8`** |
| Verification of `92c49d4` | Full CI contract passes: lock, ruff, mypy linux + win32 (src and helpers), 2950 passed / 46 skipped / 0 failed, clean tree |
| `PROJECT_STATUS.md` deadlock on the host | **Cleared** at 12:33:44, evidence preserved |
| Ingestion | **Recovered** on the currently pinned commit: 12:45 and 13:15 runs `SUCCEEDED`, no refusal |
| Live-store backup | Taken 12:33:14 with `backup-database`, verified |
| Deployment of `92c49d4` | **Not done — blocked on an operator-only step** (below). Deployed commit remains `5025b21` |
| Migration 0028 on the live store | **Not done** (depends on the deployment). Rehearsed on a copy: passes |
| PAPER boundary | Intact; no order, fill, position, reconciliation or kill-switch change |

## Why deployment stopped, precisely

Every Elrond application task runs as `LEANNES-LAPTOP\erlond`, logon type *Password*, and its action
pins the commit it may run (`-Commit 5025b21…`). `start-service.ps1` refuses a checkout whose HEAD
differs from that pin (`CHECKOUT_PIN_MISMATCH`), so activating `92c49d4` requires changing the pin.

The repository's supported way to change it is `deploy\omen\setup-windows.ps1 -Mode Boot -Credential
… -UpdateManaged`. The installer refuses Boot mode without elevation (`BOOT_MODE_REQUIRES_ELEVATION`)
and without the runtime account's password (`BOOT_MODE_REQUIRES_CREDENTIAL`), and a Password-logon
task cannot be re-registered without that password in any case. This session ran as
`LEANNES-LAPTOP\leepa`, which is not a member of Administrators, and does not hold — and must not be
given — the `erlond` password.

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
the fix's regression tests until `92c49d4` is deployed. `5025b21` still writes the file relative to the
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

These are measurements of the exact code to be deployed, not of the deployed runtime; the latter
does not exist yet and is not claimed.

## Phase 14 — PAPER boundary on the running deployment (13:16)

Byte copy after the two recovered ingestion runs versus the Phase 0 copy: the **only** changed table
is `ingestion_runs` (306→308, both `SUCCEEDED`). `order_intents` 11 (latest 2026-09-09), `broker_orders`
10, `fills` 11 (latest 2026-09-10), `positions`, `reconciliation_runs` (latest
`run-20260911T200500Z RECONCILED`), `account_snapshots` and the kill switch (disengaged,
2026-09-10T21:15:23) are unchanged. Account identity: SHA-256 prefix of `EXPECTED_ACCOUNT_ID` equals
the latest snapshot's (`e09a79ca34d782e3`). Dotenv unchanged since 2026-09-08: `TRADING_MODE=PAPER`,
`BROKER_ENVIRONMENT=PAPER`, `LIVE_TRADING_ACKNOWLEDGED=false`, `KILL_SWITCH=false`, no `ALPACA_LIVE_*`
[phase14-safety-current-deployment.txt].

## To finish: one elevated run by the operator

`D:\Elrond\evidence\deploy-2026-09-13\deploy-main.ps1` (SHA-256 `6d5d7c55…98bbcc`; its probe
`db_probe.py` `4593c00e…6138a`). From an **elevated** Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File D:\Elrond\evidence\deploy-2026-09-13\deploy-main.ps1
```

It refuses unless `origin/main` is still exactly `92c49d4`, the deployed checkout is clean, and the
PAPER settings are intact; then disables and stops the six application tasks (the model server keeps
running) and waits until no service process is alive and the WAL is quiet; takes and verifies a fresh
backup; switches the checkout to `92c49d4` (the old branch stays at `5025b21` for rollback); migrates and
verifies schema, constraints, index and row preservation; prompts for the `erlond` password with
`Get-Credential` and runs the repository installer with `-Mode Boot -UpdateManaged`; checks every task
is pinned to `92c49d4`; starts the six tasks; records processes, commits and launcher logs. Every step
writes UTC times and real exit codes to a new `run-<stamp>` directory, and any failed check stops the
run with the next safe action printed. Its read-only preflight was rehearsed as `leepa` at 12:49:32
and passed [run-20260913T124932Z-rehearsal/].

After it completes, the remaining checks are: two scheduled ingestion intervals on `92c49d4` with a
clean checkout; the first research run's proposal record on the live store (a legitimate
`benchmark-move-size-persists` proposal is expected to occur naturally — it is eligible there);
report at 21:15; and a repeat of the Phase 14 comparison.

## Follow-ups, not part of this objective

1. `tests/integration/test_generated_experiment.py` is WSL-only and never runs on the Omen.
2. `research-status` lists empty `AVAILABLE` datasets as clean confirmatory capacity.
3. Deployment evidence scripts live outside Git; if a repeatable deployment runner is wanted, it
   should be reviewed into `deploy/omen/` rather than copied from here.
