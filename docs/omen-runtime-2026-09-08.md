# Omen deployment evidence — 2026-09-08

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
