# Omen deployment and readiness — 2026-09-09

## Latest observation — 13:46 UTC (09:46 Eastern)

The deployed daemon completed actual observations at the 09:30 open and at 09:45:04 Eastern.
The latter is HEALTHY. This supersedes the preopen scheduling calculation below with observed
session activity; no manually fabricated heartbeat was used.

At the open, protection found nine broker positions while its previous local snapshot was flat,
so its first evaluation correctly recorded nine BLOCKED_STATE positions and a
POSITION_STATE_UNRECONCILED incident. The concurrent fill collection initially retained eight
fills; QQQ's stored order was still new. The single recorded reconciliation difference was account
equity 499.98 versus 499.91 across reads, and the observation also refused the incomplete ledger.

The existing reconcile command was run at 13:43 UTC. It imported all nine actual fills, updated
the terminal order states, checked the independent fill-derived quantities against the broker,
and returned RECONCILED with zero differences and no unresolved intents. The next scheduled
protection cycle at 13:45:00 UTC was HEALTHY: nine matching positions with usable prices,
zero unprotected positions and no exit required. The scheduled 13:45:04 observation also
reconciled cleanly and was HEALTHY. Protection is evaluation on the existing 15-minute schedule;
this evidence is not a claim of continuous broker-held stop orders.

At 13:45:45 UTC the specific opening position-mismatch incident was resolved through
StorageRepository.resolve_incident after transactionally checking the fresh healthy protection
cycle, all nine evaluations and clean reconciliation. A separate resolved INFO audit record
links the original incident to those measured cycle/reconciliation identifiers. The original
incident is retained. Yesterday's HTTP 403 incident remains unresolved, and IEX volume capability
restrictions remain unchanged. No code, task registration, risk threshold or kill-switch state
was changed for this recovery. Market-open snapshot races can recur; this observation does not
claim that a permanent synchronization change has been implemented.

The dashboard at 13:46 UTC reports nine protected, zero unprotected, zero reconciliation
differences, PAPER/PAPER and a clear switch. Account equity in the latest stored snapshot is
500.20. Local evidence is under `D:\Elrond\evidence`: `position-attention-runtime-audit\`,
`opening-position-incident-resolution.json`, and `position-attention-dashboard-after.json`.

## Preopen deployment observation — 12:27 UTC (08:27 Eastern)

The overnight reboot occurred at 06:12:15 Eastern. The startup service hosts returned in
session 0 approximately 13 seconds later, with Python and Ollama following at 06:12:36.
Console, daemon, protection and model tasks were Running; scheduled ingestion, research and
report completed with result 0. This is actual reboot evidence, superseding the earlier
registration-only limit below. It does not establish the timing of interactive sign-in.

The screenshot's stale-observation warning exposed a real scheduling defect despite those
running processes: an overnight daemon start selected the next after-close cycle and skipped
the coming daytime observations. The [market-hours fix](omen-market-hours-monitoring.md)
is now deployed at clean runtime commit `f125bd5fdb028177879ed9efe50f7e098af641af`.
The six application tasks were stopped, all their prior processes exited, and their startup
actions were updated to that exact commit at 12:26 UTC. Local `erlond` Password logon, Limited
privileges and AtStartup/timed triggers remain verified. Elevated process-owner inspection
confirmed `erlond` in session 0. The model server continued running; no OpenConsole was present.

The new dashboard returned HTTP 200 and displays the off-hours explanation without claiming
the daemon is probably stopped. A real PAPER broker clock read at 12:27 UTC and the deployed
scheduler selected `OBSERVE` at **13:30 UTC / 09:30 Eastern today**. This calculation is not a
completed observation. The genuine last market-session observation still dates to September 8;
no synthetic observation, trading cycle, fill or qualification day was created.

Fresh config, status, doctor, preflight, ingestion-status, research-status and reconciliation
commands returned 0 with their outputs inspected. Doctor reports ready, account verified,
risk healthy and kill switch clear; reconciliation reports zero differences and unresolved
intents. Ingestion succeeded at 12:26:36 UTC, with daily bars through September 8 at 20:00 UTC.
The active database and PAPER environment are unchanged. Research remains operational M0;
no forward-supported edge is claimed. Recovery inventory/startup reports still refuse global
authority and standalone reading of the active WAL store, as explained below.

The unresolved September 8 HTTP 403 incident and IEX single-venue volume limitation remain
visible. The original rejection body was not retained, so its exact cause is still unproven.
The switch was already clear; this deployment did not clear it again or resolve the incident.

Full Windows local CI on exact implementation commit `f125bd5` passed **2,822 tests, 45 skipped,
one warning in 625.62 seconds**, plus Ruff, Linux/win32 type checks, lockfile and clean-snapshot
checks. Linux execution was **NOT RUN / DOCKER_NOT_FOUND**, so aggregate CI returned 1.
Independent code review found no remaining actionable issue after the close-boundary and slow
observation regressions were addressed. Later documentation commits do not alter the runtime pin.

Local evidence: `D:\Elrond\evidence\reboot-20260909-verification.json`,
`market-hours-deployment.json`, `market-hours-clock-proof.json`,
`market-hours-runtime-audit\`, and `market-hours-local-ci-summary.json` under that evidence directory.
These records contain operational observations; secrets and raw account identifiers are not
included in this Git report.

## Historical deployment — 00:13 UTC

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
