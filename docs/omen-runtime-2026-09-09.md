# Omen deployment and readiness — 2026-09-09 00:13 UTC

The tested entry-budget implementation is deployed and the legitimate measured-clearance path
has cleared the durable kill switch. Native Windows Task Scheduler owns the active PAPER stack.
This follows the [September 8 evidence](omen-runtime-2026-09-08.md); the earlier canceled elevation
and halted state are historical observations, superseded by this successful retry.

## Running version and supervision

The active clean checkout is `C:\Users\leepa\elrond-omen-runtime`, branch
`codex/omen-paper-runtime`, commit `094fefc1538e46dfca1b48c2e0a1ca50857fc43e`.
Subsequent documentation commits are prepared in a separate worktree and do not change this pin.
The measured machine is HP OMEN 16-n0xxx, Ryzen 7 6800H (8 cores / 16 threads), 31.21 GiB RAM,
Windows 11 Home build 26200. The discrete RX 6650M remains disabled with device error 22; local
model execution is CPU-based. Python 3.11.16 is external to packaged-app AppData. WSL and Docker
are absent; the native Windows deployment has been tested on this host.

At 00:08–00:09 UTC the elevated deployment stopped all six application tasks and observed every
prior application process exit before updating the checkout. It repinned all six actions,
preserving local standard account `LEANNES-LAPTOP\erlond`, Password logon, Limited privileges,
AtStartup and timed triggers. Direct elevated process-owner inspection verified `erlond` and
session 0 for the application/model processes. The dashboard returned, and ingestion, research
and report completed with result 0. The model server did not need restarting for this code change.

| Task | Observed state | Schedule |
| --- | --- | --- |
| Elrond Console | Running, HTTP 200 on loopback port 8787 | Startup and 5-minute retry |
| Elrond Daemon | Running, normal market schedule | Startup and 5-minute retry |
| Elrond Protection | Running, normal market schedule | Startup and 5-minute retry |
| ElrondModelServer | Running, loopback port 11434 | Startup and 5-minute retry |
| Elrond Ingestion | Ready after successful completion | Startup and 30-minute collection |
| Elrond Research | Ready after successful completion | Startup and 6-hour maintenance |
| Elrond Report | Ready after successful completion | Startup and daily reporting |

No OpenConsole process was present. Separate cross-process acquisitions of the actual daemon,
protection and console locks were refused. The maintenance restart proves shutdown, replacement
and return of the correct services against the same store. Boot registration is verified; no
physical reboot was performed. The earlier forced-crash test measured recovery through the
5-minute retry after about 245 seconds; it did not demonstrate the configured 1-minute restart.

## PAPER readiness and durable state

The application parser verifies PAPER/PAPER, configured PAPER credentials and account identity,
no live credentials, live acknowledgement false, and `ENTRY_BUYING_POWER_RESERVE_BPS=500`.
The new budgeting behavior and its limits are described in [entry budgeting](omen-entry-buying-power.md).

At 00:09 UTC read-only broker checks found ACTIVE status, entry allowed, buying power 49.81,
nine unfilled buys and zero positions. The deployed builder retained 449.99922076 of pending
reference exposure and computed new reference buying-power budget 47.3195. The historical DBC
quantity at the stored close was approximately 50. The original HTTP 403 response body is still
unavailable; its exact cause is not proven. The cash fix and 5% project-default reserve mitigate
the demonstrated batch funding flaw without guaranteeing acceptance at future market prices.

At 00:10:28 UTC `quantbot kill-switch clear --reason ...` ran its real `measure_readiness()` path
and returned `engaged=false`. Fresh `doctor` and `reconcile` then returned 0: account verified,
risk healthy, trade-stream check connected, readiness true, no safety reasons, zero reconciliation
differences and no unresolved intents. No SQL flag edit, declared readiness substitute, canceled
order or rejected-intent replay was used. The nine previously accepted orders remain pending;
clearing or engaging the switch does not itself cancel them.

The active store remains `D:\Elrond\state\quantbot-paper-fresh-20260908.db`, schema 27 / Alembic
0027, UUID `cd2a5911-853a-484e-bbc8-d0c27b5d27aa`. It belongs to the fresh PAPER account supplied
by the operator and was reconciled before activation. No missing prior forward history was
migrated. The original `quantbot-paper.db` is preserved separately. An online backup at
`D:\Elrond\backups\quantbot-paper-entry-budget-20260909.db` passed `integrity_check`, retained the
same UUID/schema and clear switch, and contained one run, ten intents, zero fills/positions,
6,992 bars, one runtime-deployment observation and zero qualification days. The ERROR run and
nine accepted/one rejected intents are retained as actual history.

## Current activity and verification limits

Fresh `config`, `status`, `doctor`, `preflight`, `ingestion-status`, `research-status` and
`reconcile` all returned 0 with their semantic outputs inspected. Ingestion's latest scheduled
run succeeded at 00:09:55 UTC, with healthy collection through September 8 at 20:00 UTC and no
open incident. Research reports an empty queue and operational M0; no forward-supported edge is
claimed. A real `qwen3:4b-instruct` request returned `OK` in 3.49 seconds. The earlier real-endpoint
generator/critic and isolated Kronos checks remain recorded in the September 8 report.

The market is closed. The latest market-session observation remains the genuine 19:52 UTC
DEGRADED observation from September 8; the latest protection observation is the genuine 19:52 UTC
HEALTHY observation with zero positions. Running processes and a fresh readiness probe are not
new market-session or protection observations. No artificial cycle, fill or qualification day was
created to make a heartbeat look recent. The services await their normal market schedule.

Read-only recovery inventory/startup selection still exit 1. Their restricted scope does not
probe the broker, infer cross-host fencing, or read an active SQLite WAL as a consistent standalone
database. The explicit configured path, verified online backup, real reconciliation and measured
task/process identity provide separate operational evidence. These tools' refusals were not
patched away. The Omen is the only configured local coordinator and the sole permitted host;
the Pi is retired by operator policy and deliberately unprobed. No Pi or Alienware runtime
dependency remains, but global fencing of uninspected historical hosts is not claimed.

Full Windows local CI on exact implementation commit `094fefc` passed **2,815 tests, 45 skipped,
one warning in 521.45 seconds**, plus Ruff, Linux/win32 type checks, lockfile and clean-snapshot
checks. Linux execution was **NOT RUN / DOCKER_NOT_FOUND**, so aggregate CI returned 1. No hosted
Actions were dispatched. Focused risk/config/runtime checks passed 353 tests and independent
review found no actionable issue. Documentation follow-ups receive diff and secret checks;
application tests are not repeated solely for prose changes.

Evidence is retained outside Git under `D:\Elrond\evidence`: `entry-budget-deployment.json`,
`entry-budget-task-verification.json`, `entry-budget-kill-clear.json`,
`entry-budget-final-runtime-audit`, `entry-budget-readonly-proof.json`,
`entry-budget-model-inference.json`, `entry-budget-store-verification.json`,
`entry-budget-host-verification.json`, `entry-budget-final-console-health.json`,
`entry-budget-local-ci-summary.json` and `entry-budget-local-ci.log`.

The implementation and current documentation are in [PR #228](https://github.com/jfhutchi/project_elrond/pull/228),
left open and unmerged. Secrets, operational databases and raw logs remain outside Git.
