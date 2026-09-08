# Omen deployment evidence — 2026-09-08

## Windowless supervision and crash recovery — 18:49 UTC

Commit `f1c440b0e2d6c5ce0db7ecffe5726be4beff538a` replaces the scheduled task's console executable
with a small GUI executable that starts fixed system PowerShell without allocating a console.
The host owns a kill-on-close job before creating children, preserves exit codes and argument
quoting, and reports startup output to external launcher logs. An asynchronous logging failure
deliberately exits 70 and closes the process tree instead of raising an unhandled GUI exception.
The existing validated PAPER wrappers and their nested jobs remain in use.

At 18:39 UTC all six application tasks and `ElrondModelServer` were switched to the host at
`D:\Elrond\state\service-host\1188bba2cb38de299aad0768\elrond-service-host.exe`.
All eleven previous service/model processes exited, and all four old `OpenConsole` processes
disappeared. Four persistent GUI hosts ran after maintenance completed; no replacement console
process was created. Console HTTP and the model API were reachable; PAPER mode, clean
reconciliation and healthy protection remained verified.

A controlled hard termination of the console host at 18:40:59 UTC removed its entire five-process
tree. The configured one-minute `RestartOnFailure` did not recover this forced termination.
The five-minute recurring trigger recovered it automatically at 18:45:04 UTC, about 245 seconds
later, with a new process tree and zero console windows. No manual start was issued between
failure and recovery. The daemon and protection continued running during this dashboard-only
test. Recovery within one minute is therefore **not** claimed; the recurring trigger is the
measured fallback. Process restart is verified, while unattended startup before Windows sign-in
still requires unavailable Windows privileges/credentials.

Fresh application-parser checks verify PAPER/PAPER, PAPER credentials and the expected account
configured, no LIVE credentials, and live acknowledgement false. `config`, `status`, `preflight`,
`ingestion-status`, `research-status` and `reconcile` return 0. `doctor` returns 2 solely for the
unchanged daily-bar freshness warning. Read-only `deployment-status` recovery reports still
refuse global authority and automatic recovery selection: remote fencing is unverified and the
active SQLite WAL requires a consistent snapshot. No recovery authority was manufactured.
A new online SQLite backup at
`D:\Elrond\backups\quantbot-paper-windowless-f1c440b-20260908.db` passes `integrity_check` and
reports Alembic `0027`. AC idle sleep and hibernation are already disabled; no power setting changed.

Full Windows local CI on `f1c440b0e2d6c5ce0db7ecffe5726be4beff538a` passed:
**2,808 passed, 45 skipped, one warning in 554.64 seconds**. Ruff, Linux/win32 type checks,
lockfile and clean-checkout checks passed. Linux remains `NOT RUN: DOCKER_NOT_FOUND`, so the
aggregate command returns 1. The five new host tests cover GUI subsystem/no console, stream
output and exit codes, argument preservation, descendant teardown, startup log refusal and
controlled callback failure. Independent review found no remaining blocker after the callback
fix. Evidence-only commits after this snapshot do not change implementation.

Evidence: `D:\Elrond\evidence\windowless-first-deployment.json`,
`windowless-automatic-recovery.json`, `windowless-backup.json`, `windowless-runtime-audit`,
`windowless-local-ci.log` and `.local-ci/20260908T183855Z-8e905766/summary.json`.

## Dashboard market-data sync feedback — 18:12 UTC

Commit `71d8ae03a075917282cb14db8c8882cad1cf3851` fixes a misleading console action:
the sync callback ran, but its result was discarded before redirecting to an unchanged page.
The page now displays actual additions, zero additions, completed-through time, stale symbols
and failure reasons, including unhealthy returns and partial progress. During submission the
button displays progress and disables repeat clicks. The collector and safety gates are unchanged.

After deployment, an actual click in the operator's browser completed successfully and displayed
"No new bars were added" and completed-through `2026-09-04T20:00:00+00:00`. The store already held
6,969 daily bars across 23 symbols. Earlier checkpoint conflicts were also real sync results;
those will now be visible instead of silently returning to the same page.

All six application tasks were repinned and restarted through Task Scheduler. All six prior
Python processes exited before replacement. At 18:13 UTC the console, daemon and protection
were running; ingestion, research and report tasks had completed with result 0. The console
reported PAPER/PAPER, a clear kill switch, clean reconciliation with zero differences, and
healthy protection with zero positions. Four real market-session observations had been recorded;
each reported `DEGRADED` / `MARKET_DATA_STALE` under the existing daily-bar wall-clock check.
No trading run or qualification day is claimed.

The 162 console tests passed, including regression coverage for new bars, no new bars, unhealthy
results, partial progress and escaped error text. Ruff and mypy passed; independent review found
no blocking issue. Runtime evidence is retained in
`D:\Elrond\evidence\market-data-sync-restart.json` and
`D:\Elrond\evidence\market-data-sync-console-state.json`.

The full local CI uses the shared `scripts/local_ci.py` runner with the explicitly installed
`D:\Elrond\tools\python\cpython-3.11.16-windows-x86_64-none\python.exe` interpreter (`-I -S -B`).
The PowerShell bootstrap did not discover that external managed-Python installation. Linux
verification remains unavailable because Docker is not installed; no hosted Actions are used.

The full Windows job on `71d8ae03a075917282cb14db8c8882cad1cf3851` passed:
**2,803 passed, 45 skipped, one warning in 570.34 seconds**. Ruff, Linux/win32 mypy, lockfile
and checkout checks passed, and the disposable workspace cleaned up successfully. The aggregate
command returns 1 because Linux was requested but could not run. Logs are retained at
`D:\Elrond\evidence\market-data-sync-local-ci.log`; the machine-readable result is
`.local-ci/20260908T181106Z-ac062b07/summary.json`. Subsequent evidence edits do not change
application code.

## Fresh PAPER account activation — 16:59 UTC

The operator supplied credentials for a newly created PAPER account. The application adapter
authenticated it as a different, active account with zero historical fills, open orders, or
positions and no reported restrictions. Its verified account ID and credentials remain only in
the protected external environment file. The prior account and all its local files were preserved.

A separate ledger was created through the supported CLI at
`D:\Elrond\state\quantbot-paper-fresh-20260908.db`, schema 27, store identity
`cd2a5911-853a-484e-bbc8-d0c27b5d27aa`. It has its own account lineage and 25 seeded institutional
findings; it inherits no prior forward history. Ingestion stored 6,969 real daily bars. Reconciliation
now returns exit 0, `RECONCILED`, zero differences, no unresolved intents and no blocking reasons.

At 16:54:07 UTC the normal `kill-switch clear` command succeeded. A further application readiness
measurement at 16:56:50 UTC verified broker health, account identity, reconciliation, market data
and risk configuration, with no blocking or unprobed conditions. Only after this measurement and
durable clearance were the documented process-level operator gates enabled and `KILL_SWITCH`
set to false in the external configuration. No gate implementation or freshness threshold changed.
Both modes remain PAPER and live acknowledgement remains false.

The console, PAPER daemon, independent protection process and model server are running through
Task Scheduler. Ingestion, research and reporting are enabled and have each completed with task
result 0. Protection recorded a `HEALTHY` pass with zero positions. A supervisor stop/restart
replaced all six prior application Python processes; none remained after shutdown settled.
The console reports the same fresh store with a clear switch and clean reconciliation.

`doctor` still reports `MARKET_DATA_STALE` under its 24-hour wall-clock check: September 4 remains
the newest completed daily session before today's close. The collector reports no missing/stale
symbols against the market calendar. The unchanged after-close cycle fetches completed daily
data and applies its own safety gates before evaluating orders. No new trading run or qualification
day is claimed from starting the services; the first daemon observation is still pending.

This supersedes the recovery blocker described in the earlier snapshots below. The remaining
startup limitation is unchanged: tasks survive/restart within a signed-in session, but startup
before sign-in requires Windows privileges/credentials unavailable to this process. The Pi is
retired, and no other computer is a runtime dependency.

Evidence: `D:\Elrond\evidence\fresh-paper-account-verification.json` and
`D:\Elrond\evidence\fresh-paper-activation`. Application code is unchanged from the full Windows
CI run recorded below; this activation changes external configuration and deployment evidence.

## Credential activation follow-up — 15:49 UTC

The operator subsequently supplied PAPER credentials. They were saved only in the existing
restricted external environment file, and authenticated through the application's Alpaca PAPER
adapter. The account reported `ACTIVE`, with no trading restrictions. Its returned account UUID
was pinned as `EXPECTED_ACCOUNT_ID`; no credential or account identifier is published here.

The account is not empty: the broker returned seven historical fills spanning August 17–18,
seven open PAPER positions, and zero open orders. The fresh Omen ledger has none of their original
order intents or broker orders. Normal reconciliation refused with `StateConflictError` because
a historical fill references an unknown broker order. The repository requires broker orders to
link to durable order intents. No intents, fills, strategy history, or reconciled account state
were invented, and no broker position was modified. Recovering the original ledger, or selecting
a separate unused PAPER account, remains necessary; the current account was not reset.

The ordinary `kill-switch clear` path was attempted after authentication and refused with
`RECONCILIATION_REQUIRED`. The switch remains engaged. `doctor` also reports `MARKET_DATA_STALE`
under its wall-clock age check: the newest completed daily session available is September 4,
before the holiday weekend. Collection itself succeeded, storing 6,969 real IEX daily bars and
reporting no stale symbols. No risk/readiness flag was manually marked healthy.

Activation exposed relative lookups for the exposure classification and economic mandate when
running from the external state directory. Commit `cee0a4d400d773ca00043b7ebe9bfc57ff4aa532`
binds both default policy paths to the checkout supplying the module. Explicit caller paths still
retain their meaning. Regression tests cover an external working directory and a conflicting
local policy file; 347 relevant tests, Ruff, and Linux/win32 type checks passed.

Full local CI on that application commit passed Windows: **2,801 passed, 45 skipped, one warning**
in 561.54 seconds; Ruff, both mypy platforms, lockfile and checkout checks passed. Linux remained
`NOT RUN: DOCKER_NOT_FOUND`. The outer command exited 1 and also reported a temporary-directory
cleanup error: a negative launcher test's `.txt` fixture opened Notepad through file association.
The fixture was changed to an invalid `.exe`, and all four launcher tests plus Ruff passed again.
No application code changed after the full-suite snapshot. Automatic approval review blocked
closing the test-created editor/removing the retained scratch directory; the logs preserve that
cleanup failure. Full-suite evidence: `.local-ci/20260908T154801Z-e6e10366/summary.json` and
`D:\Elrond\evidence\paper-activation\local-ci.log`.

The console and model server are running. Ingestion is now enabled on its 30-minute schedule and
has completed through Task Scheduler with result 0; research remains enabled on its six-hour
schedule. Trading, protection and account reporting remain disabled pending reconciliation.
The seven existing PAPER positions are therefore not under this deployment's position protection.
SQLite integrity is `ok`; local fills, broker orders, order intents, reconciliation runs, trading
runs and qualification days remain zero. Credentials did not repair the independent Windows
startup-permission blocker: supervision still requires a signed-in session.

The supported backup command preserved the post-ingestion store at
`D:\Elrond\backups\quantbot-paper-post-ingestion-20260908.db` (schema 27, 3,227,648 bytes,
zero trading runs and qualification days). The original pre-ingestion backup is also retained.

External evidence is under `D:\Elrond\evidence\paper-activation` and
`D:\Elrond\evidence\paper-account-verification.json`. The sections below preserve the initial
deployment snapshot; this follow-up supersedes their absent-credential and disabled-ingestion
statements.

## Initial deployment snapshot

**Partial deployment, not a running paper-trading account.** The Omen is the sole intended
coordinator. The Pi is retired and has no current role. No other computer was contacted or made
a dependency. Broker activation is blocked by absent PAPER credentials; unattended startup
before login is blocked by Windows task-registration permissions. Neither condition is a pass.

## Measured host and architecture

`LEANNES-LAPTOP`: Windows 11 Home 10.0.26200, HP OMEN 16-n0xxx, Ryzen 7 6800H,
8 cores / 16 threads, 33,507,008,512 bytes RAM (31.2 GiB). Windows detects the Radeon RX 6650M
and integrated Radeon graphics. RX 6650M has device error 22 (disabled); CPU execution alone is verified. Two NVMe disks: WD_BLACK SN850X 1 TB and Samsung
MZVL21T0HCLR 1.024 TB. Initial free space was approximately 839 GB on C: and 999 GB on D:.
AC automatic sleep was already disabled; battery sleep remains 600 seconds. No reboot performed.

Native Python 3.11.16 is managed by uv 0.12.7. Its runtime installation is
`D:\Elrond\tools\python\cpython-3.11.16-windows-x86_64-none`; the isolated checkout venv
was rebuilt from that interpreter with the unchanged lockfile. WSL reports not installed, no distributions or
systemd guest exist, and Docker is absent. `wsl --install --no-distribution --no-launch` returned
exit 1 without installing. Windows rejected boot and S4U task registration with `Access is
denied`; current-user interactive task registration succeeded. Temporary permission probes were
removed. These are measurements on this laptop, not the historical desktop named Omen.

Native Task Scheduler is the available supervisor for the existing cross-platform Python core.
Separate processes isolate trading, position protection, console and maintenance. The installer
pins a clean Git commit and external database/configuration paths. Logon and periodic triggers
recover tasks within a signed-in session. **That is not startup before login.** Protected
Task Scheduler boot credentials/elevated registration remain an operator access step. WSL/systemd
would supply the existing Linux sandbox but needs installation/access and its own Windows boot
activation; lingering alone does not start a stopped WSL distribution. Docker would add an
unavailable runtime dependency without improving the existing single-writer SQLite architecture.

The original AppData interpreter was visible inside packaged Codex but mapped to its private
`LocalCache\Roaming` directory. That namespace was unsuitable for ordinary scheduled tasks.
The leading quote in Python's “No Python at” diagnostic is part of CPython's error formatting,
not proof of a malformed task argument ([CPython launcher source](https://github.com/python/cpython/blob/3.11/PC/launcher.c)). Moving the interpreter to explicit external storage
removes that packaged-app path dependency.

## Store provenance and controls

Selected fresh store: `D:\Elrond\state\quantbot-paper.db`, lineage
`2244c782-29e9-4ab0-bff8-bced024a226b`, schema 27 / Alembic `0027`.
Created deliberately through `quantbot create-database --confirm`. **Prior forward history was
not migrated.** Initial counts: zero runs, fills and qualification days. The 25 seeded research
findings are checked-in institutional history, not newly performed experiments.

Recovery used `quantbot deployment-status` before writable runtime commands. Bounded scans
covered D:, the user profile, OneDrive, Documents, Downloads, prior recovery directories,
ProgramData, Public, `.claude`, AppData/Roaming and Windows.old/Users. The whole-profile scan
hit its cap in packaged dependencies; targeted operational scans did not. A final uncapped
`rg --files --hidden --no-ignore` profile pass found 191 database-extension paths, with no
additional operational Elrond candidate and no reported enumeration errors. Unreadable system
directories and excluded dependencies remain explicit in local inventory JSON. Windows.old held
774 Elrond-schema databases, all pytest temporary fixtures. `tests/conftest.py` establishes their
`deployment*` fixture provenance; those candidates are not operational ledgers. No previous
operational store was identified. Historical Alienware/Pi account evidence was not accessed.

Initial backup: `D:\Elrond\backups\quantbot-paper-initial-20260908.db`. Both files passed
`PRAGMA integrity_check`; SQL dumps were identical and lineage matched. SQLite backup changes
physical file bytes, so different file hashes were not mistaken for a logical mismatch.
Local provenance is in `D:\Elrond\evidence\store-provenance.json`.

External environment: `C:\Users\leepa\.config\quantbot\quantbot.env`, ACL restricted to the
owning user, SYSTEM and Administrators. Actual `Settings.safe_summary()` reported PAPER trading
and broker environment, live acknowledgement false, no live credentials, no PAPER credentials,
and all readiness assertions false. No values were taken from chat or embedded in a task.

Durable kill switch: **ENGAGED**, reason `default fail-closed state`; process switch true.
No clearance attempted because account, broker, data and reconciliation readiness are absent.
The supported recovery path ingests real PAPER broker orders/fills and compares positions derived
from fills with broker positions. Missing older fills must produce `POSITION_LEDGER_MISMATCH`,
not a manually invented balance or reconstructed qualification history.

## Initial application observations

| Command | Actual result |
| --- | --- |
| `config` | Exit 0; application configuration registry captured outside Git |
| `status` | Exit 0; default kill switch engaged, PAPER-only, credentials absent |
| `doctor` | Exit 2; `PAPER_CREDENTIALS_NOT_CONFIGURED` |
| `preflight` | Exit 1; PAPER trading, market data and model routing initially unconfigured |
| `reconcile`, `sync-data` | Exit 2; `PAPER_CREDENTIALS_NOT_CONFIGURED` |
| `ingestion-status` | Exit 0 with **zero sources**; nothing collecting, not healthy ingestion |
| `research-status` | Exit 0; prior memory present, no tasks/edges; initially missing model routing |
| `deployment-status` | Recovery report, exit 1; global authority and broker reconciliation unverified |

No broker connection, current positions, fresh market data, forward cycle or ingestion heartbeat
has been demonstrated without credentials. The PAPER endpoint is fixed in the Alpaca paper
adapter; successful authentication/reconciliation is a separate still-unmet acceptance condition.

## Local compute

The existing isolated Kronos v2 CPU worker ran twice through the current repository qualification
tool against its explicitly synthetic diagnostic corpus, offline. Exact dependency/artifact pins,
clean upstream code and absence of `quantbot` in the worker environment were verified. Wall time:
23.59 / 10.43 seconds; peak process memory 479.2 / 480.5 MB. All 240 values matched across runs.
No worker remains resident. This proves execution/repeatability only, not forecasting skill or
market evidence. Details: `C:\Users\leepa\elrond-worker-check-20260908\qualification-report.md`.

After fixing the packaged-app path dependency, the physical Kronos worker venv was rehomed to
identical Python 3.11.16 on D: by changing only its `pyvenv.cfg` home line, preserving a backup.
A normal Interactive Scheduled Task then ran two offline diagnostics: 33.657 / 14.445 seconds,
peak memory 481.1 / 479.1 MB; all 240 values identical. The diagnostic task was removed and no
worker remained. Evidence: `D:\Elrond\evidence\kronos-task-20260908T103928\qualification-report.md`.
This separately proves execution outside Codex's process namespace.

Portable Ollama 0.33.3 was downloaded from the official release and SHA-256 verified
(`52cb36a62e7e501f61514f60212dec7117b6c098811357585e02fffe32d2fcd7`).
`ElrondModelServer` owns the loopback-only server at `127.0.0.1:11434`. Cloud use is disabled,
parallelism/max-loaded-models are one, context is 8192, and keep-alive is zero. It measured CPU
inference; no GPU execution is claimed. Initial Qwen3 4B timed out under the application's
60-second bound and Gemma3 4B failed a structured-output smoke check; neither failure is a pass.

Selected generator `qwen3:4b-instruct` and distinct critic `qwen2.5:3b` completed the repository's
four real-endpoint integration tests in **24.85 seconds**. A separate production `ModelCritic`
diagnostic returned a valid critique with two reasons, five objections and provenance in
**30.14 seconds**. It was explicitly hypothetical and was never inserted into the research ledger.
This establishes the runtime contract, not the quality of trading judgments. The selected model
digests and sizes are recorded in `D:\Elrond\evidence\selected-models.json`.

Initial Gemma and Qwen2.5 requests failed strict JSON validation. The runtime requested structured
capability but never sent the schema to the endpoint. The fix sends the supported schema request,
records it in provenance, and retains local JSON/schema validation; plain calls are unchanged.
Non-JSON/schema critic errors now become typed refusals, and rejected reviews cannot retain
provenance. Fifty focused model/critic/composition tests passed, as did independent code review.
External scheduled research configuration now uses the verified loopback generator/critic pair.
No model or worker holds broker credentials, and no worker availability grants trading authority.

Sources: [Ollama Windows deployment](https://docs.ollama.com/windows),
[model residency controls](https://docs.ollama.com/faq),
[structured-output schema support](https://docs.ollama.com/capabilities/structured-outputs),
[Windows task security contexts](https://learn.microsoft.com/en-us/windows/win32/taskschd/security-contexts-for-running-tasks).

## Supervision and final application observations

All seven tasks are current-user Interactive / Limited, with logon and periodic triggers.
Task Scheduler owns the PowerShell wrapper, venv redirector and base Python process chain;
it does not descend from the Codex shell. A shared Windows Job Object owns native descendants.
The interpreter is physically visible outside packaged Codex, as a separate same-SID scheduled
probe confirmed: logical AppData Python absent, physical package and external D: Python present.
That temporary probe was removed.

| Task | Observed state | Trigger / result |
| --- | --- | --- |
| `Elrond Console` | Running; loopback `http://127.0.0.1:8787` | Logon + 5-minute retry; home, risk and kill-switch pages HTTP 200 |
| `Elrond Research` | Ready between completed jobs | Logon + 6-hour interval; task result 0, empty queue, no fabricated tasks |
| `ElrondModelServer` | Running; loopback `127.0.0.1:11434` | Logon + 5-minute retry; API version 0.33.3, zero models resident after requests |
| `Elrond Daemon` | Disabled, never activated | PAPER credential pair absent |
| `Elrond Protection` | Disabled, never activated | PAPER credential pair absent |
| `Elrond Ingestion` | Disabled, never activated | PAPER credential pair absent |
| `Elrond Report` | Disabled, never activated | PAPER credential pair absent |

At code commit `5b936520bee130b9067270a709874018064a5e40`, an explicit duplicate console launch
returned exit 4 and a repeated task start kept listener PID 15364. Supervisor stop removed all
three service processes and the listener after bounded shutdown waiting. Supervisor restart
created listener PID 15800, returned HTTP 200, and retained PAPER mode and the engaged kill switch.
Model stop likewise removed its wrapper and PID 2748; restart created PID 14592. These are recorded
observations, not promises that PIDs remain fixed. Task definitions are exported outside Git.

Final `config`/`status` parsing still reports PAPER/PAPER, live acknowledgement false, no live
credentials and the engaged default kill switch. `preflight` now marks RESEARCH and CONSOLE
configured; PAPER_TRADING and MARKET_DATA remain unready. `doctor`, `reconcile`, `sync-data` and
`protection-daemon` refuse `PAPER_CREDENTIALS_NOT_CONFIGURED`. `ingestion-status` has zero sources.
Research has zero tasks and zero actionable transitions. Optional generated-code execution needs
the unavailable Linux/WSL sandbox; successful model calls are not proof of that sandbox.

`deployment-status --inventory --startup-check` exits 1. It identifies the selected D: database and
retired Pi policy but retains `GLOBAL_COORDINATOR_AUTHORITY_UNVERIFIED`,
`OTHER_HOST_RETIREMENT_UNVERIFIED`, `KILL_SWITCH_ENGAGED` and `BROKER_RECONCILIATION_NOT_PROBED`.
Local process exclusion and an operator's placement decision do not invent a global broker fence.
The same store UUID and schema remain readable; final integrity check is `ok`. Counts remain zero
for runs, fills, bars, orders, positions, reconciliation runs, ingestion runs, qualification days
and runtime deployments, with 25 seeded research records. No broker/risk heartbeat is claimed.

## Verification

Final implementation snapshot: `5b936520bee130b9067270a709874018064a5e40` on
`codex/omen-paper-runtime`. Subsequent evidence-only documentation changes do not change this
verified implementation; managed service actions are repinned to the final clean checkout.

`python scripts/local_ci.py --platform all`:

- Windows **PASS**: **2,799 passed, 45 skipped, one warning**, 554.13 seconds.
- Lockfile, Ruff, mypy Linux and win32 targets, CI-helper checks and snapshot cleanliness passed.
- Linux **NOT RUN**, `DOCKER_NOT_FOUND`; combined command exits 1 rather than hiding the missing
  platform. WSL is absent too. No hosted GitHub Actions were dispatched.
- Skips cover absent WSL/generated-code sandbox, optional PostgreSQL, real-model endpoints isolated
  out of CI, symlink privilege and isolated-home Python bootstrap. The four real-model tests were
  separately executed successfully against the actual local endpoint (24.85 seconds).
- Independent final review found no blocking issue. All 29 changed files were text, targeted
  added-line credential scanning found no matches, and `git diff --check` passed.

Local CI record: `.local-ci/20260908T144208Z-b4f8486a/summary.json`; a copy and the complete log are
under `D:\Elrond\evidence`. The original checkout remains clean on main
`adc61399e9387c1b43c6e28e1e179c1acf2097cd`; its files were not replaced. The branch began at reviewed
`origin/main` `d059ff87ccd856963f10c6c42f8f8ba9765330b2`.

## Remaining activation steps

1. Supply real `ALPACA_PAPER_API_KEY`, `ALPACA_PAPER_API_SECRET` and verified `EXPECTED_ACCOUNT_ID`
   through the restricted external environment file. Then measure actual PAPER readiness and
   reconcile real orders/fills/positions before any autonomous order generation. Keep the kill
   switch engaged until the legitimate measured-clearance path permits release. No older ledger
   or missing broker history can be reconstructed by assertion.
2. An elevated Windows operator must register protected startup tasks for operation before login;
   this account received Access denied. Current supervision persists within a signed-in session
   and starts at sign-in. No system reboot or automatic login was used as a substitute.

The deployment is therefore partial: console, scheduled research maintenance and local compute
are measured; continuous PAPER trading, broker reconciliation, fresh ingestion and active position
protection remain unverified and disabled. The Pi and Alienware have no runtime dependency role.
