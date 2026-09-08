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

Native Python 3.11.16 is managed by uv 0.12.7. WSL reports not installed, no distributions or
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

## Verification

Baseline `d059ff87ccd856963f10c6c42f8f8ba9765330b2`, committed snapshot local CI:
Windows **PASS**, 2,770 passed / 45 skipped / one existing warning, 533.72 seconds. Lock check,
lint, mypy Linux and win32 targets and checkout cleanliness passed. Linux **NOT RUN**:
`DOCKER_NOT_FOUND`. Skips include absent WSL/generated-code sandbox, optional PostgreSQL,
unconfigured real-model endpoints, symlink privilege and isolated-home Python bootstrap.
No hosted GitHub Actions were dispatched. Final change verification and supervision observations
will be recorded after installation; this baseline is not evidence that the new launcher passed.
