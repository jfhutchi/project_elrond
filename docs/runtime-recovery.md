# Recover runtime truth before deployment (#190)

Use `deployment-status` before `doctor`, the ordinary supervisor, any installer, or any
database command. `doctor` probes the broker and builds the runtime; construction can persist
deployment/policy metadata. The ordinary supervisor can restart a daemon. Neither is a
read-only inventory tool.

For cross-host capture with no dependency installation, use the
[Alienware/Pi evidence handoff](runtime-evidence-handoff.md). It supplies exact commands,
offline Markdown/JSON comparison, evidence requirements and retirement prerequisites.

## Read-only recovery commands

From the checkout to inspect, using its existing environment:

```powershell
uv run quantbot deployment-status --inventory
uv run quantbot deployment-status --inventory --scan-root D:\Elrond --scan-root D:\GitHub
uv run quantbot deployment-status --candidate C:\state\quantbot.db --candidate D:\archive\quantbot.db
uv run quantbot deployment-status --intended-commit <full-approved-SHA> --startup-check
uv run python scripts/supervisor.py --inventory
```

These commands return JSON and **exit 1 while recovery authority is unverified**. This is
expected even for a readable store. `--startup-check` reports blockers and never launches
anything; it cannot grant startup permission. There is no bypass/approval flag. Its output
does not replace actual broker readiness, reconciliation, or exclusion of other hosts.
`uv run` can synchronize a development environment; use the already installed `quantbot`
entry point if even dependency installation is outside the intended inventory scope.

`--repo` selects the Git checkout to compare. Database/config/lock paths still resolve from
the current process environment and working directory, exactly as runtime paths do. A repo
argument is **not** an environment switch. `QUANTBOT_STATE_DIR` is a documented future layout,
not currently a runtime path resolver. Set the actual `QUANTBOT_DB_PATH` or
`QUANTBOT_DATABASE_URL` deliberately; both set is ambiguous and refused.

Default intent is the **local** `origin/main` reference. The command never fetches and labels
that limitation. Fetch before a planned deployment, separately, or supply a full approved SHA.
The report separates the inspector's checkout from the last recorded coordinator's commit.
Comparison states: `MATCH`, `BEHIND`, `AHEAD`, `DIVERGED`, `MISMATCH` (different commits but
ancestry unavailable), and `UNKNOWN`. Only established ancestry proves `BEHIND`. A rollback
can be deliberate; mismatch is still visible. The historical A → B → A warning remains.
Git inspection disables optional locks, including index refresh writes.

## What the report can and cannot establish

* Store candidates include the configured target plus explicitly supplied paths and bounded
  scan roots. Scans report limits, unreadable directories and exclusions; they do not claim
  filesystem-wide absence. Symlinks are not followed. Dependency/cache directories are pruned.
* Each candidate reports path, size, timestamps and physical file identity; schema/revision;
  store-lineage UUID where available; latest run, deployment/config identity, reconciliation
  and hashed account lineage; bounded historical qualification rows; and the kill-switch flag.
  Qualification rows are stored claims, **not independently validated forward evidence**.
* At the current schema, the existing `DeploymentRegistry` and `IngestionControlPlane` readers
  supply runtime identity, last successful cycle, per-source/stream freshness and disabled,
  failed, stale or healthy collection states. Session observation timestamps are also exposed.
  No new heartbeat or duplicate ingestion schema is introduced. Old schemas expose the columns
  they actually have; unavailable fields are null/`UNKNOWN`, never invented zeros.
* `active` in the legacy deployment subsection means **recently observed**, not OS-verified
  liveness. `process_liveness`, coordinator authority and store authority remain separate.
  Worker observations/availability do not confer coordinator authority. The inventory process
  identifies itself as `TOOL`; Windows process ancestry identifies its own wrapper chain.
* Windows inspection reports matching processes, tasks and services, Run/RunOnce value names
  with reference matches, Startup filenames, persisted variable names (values only for the
  allowlisted non-secret runtime paths), and
  registered WSL distributions. Unknown access is reported, not converted to an empty list.
  Startup shortcuts/custom wrappers require review. WSL guests are never started to inspect
  them. Linux reports visible `/proc` processes, named system/user systemd unit files,
  bounded startup-file reference/hash scans and current-user crontab reference/hash evidence;
  other users and indirect wrappers remain unknown. No endpoint availability is invented.
* SQLite reads use URI `mode=ro&immutable=1`, `query_only`, and untrusted-schema restrictions.
  Nonempty WAL or rollback-journal files cause **content inspection to be refused**: reading
  only their base file could omit committed state. Source metadata changes during inspection
  discard the evidence. This is a bounded snapshot, not a quiescence proof. For an active/WAL
  store, obtain a separately authorized consistent SQLite backup, preserving its provenance;
  never delete sidecars or force a checkpoint to make inventory pass.
* The inventory does not construct `Database`/runtime, create directories, acquire/test locks,
  connect to brokers/workers, migrate, record observations, change startup, or clear controls.
  Arbitrary JSON details, command lines, credential values and kill-switch reasons are omitted;
  permitted text is redacted too. Do not publish credential files or unfiltered service output.

## Authority and restart limits

One plausible candidate is still `UNKNOWN`. Two physical candidates with recorded operational/research
history yield `AMBIGUOUS`, even if one is configured or newer. Identical copied lineage UUIDs
do not collapse two files into one; hard-link aliases do. Filesystem mtime never chooses a store.
Compare real run/reconciliation/account/config/forward lineage across **all previously used
hosts**, then preserve backups and document the measured selection before migration.

The writer lock excludes concurrent cycles sharing that lock file. The daemon lock excludes
local schedulers sharing its configured path. Neither fences a Pi or stale Windows checkout
using another lock/store against the same broker account. Existing lock files contain a NUL
byte, not owner PID/start metadata. Inventory deliberately reports owner/held as `UNKNOWN`
rather than acquiring the lock (which can create it) or claiming a PID owns it from coincidence.

**This change does not solve cross-host fencing or retire old installations.** The older
supervisor, Task Scheduler entry, and Pi units remain capable of starting old code if enabled.
Current recovery preflight always refuses startup authority because it cannot verify global
retirement. It is an evidence report, not a new enforcement plane. The updated supervisor also
refuses its own automatic restart when cwd differs from its source checkout, the checkout is
dirty, or HEAD cannot be verified against local `origin/main`. This closes a local stale/wrong
checkout restart path **only for supervisors running this code**; it does not fence old copies,
direct `quantbot daemon`, Task Scheduler or systemd launches. Local origin/main can itself be
outdated, so fetching and approval of deployment intent remain separate operator work.
Never interpret a clean local process list or a green
historical deployment subsection as permission to start a second coordinator.

After authority is established: preserve a consistent backup; use the intended reviewed code
and locked environment; explicitly rehearse the migration on that backup; then deliberately
migrate the selected store. Verify PAPER settings and account identity, actual broker
reconciliation and readiness, the kill switch's underlying condition, single-scheduler
exclusion and autostart retirement before starting. Reboot recovery is a later deliberate test.
No step here authorizes LIVE or automatically clears the kill switch.

## Pi checks when access becomes available

`PI_STATE=UNKNOWN` until measured. Use an already verified SSH host alias/key or physical access.
Do not guess addresses, accept an unverified key, install credentials, or run `deploy/pi/setup.sh`.
Use the exact dependency-free commands in the [evidence handoff](runtime-evidence-handoff.md#pi-exact-commands-at-the-physical-console-or-over-already-trusted-access).
They preserve stderr and scan coverage. If compatible Python or access is unavailable, retain
UNKNOWN database evidence and return the OS metadata/errors; do not repair the runtime to inspect it.

## Measured Omen snapshot — 2026-09-05

Host `Leannes-Laptop`, HP OMEN by HP Gaming Laptop 16-n0xxx, Windows 11 Home 10.0.26200,
AMD64. WSL reports not installed. Before implementation, no Python/uv/QuantBot runtime was
found, no matching scheduled task/service or Run startup reference was found, and both Startup
folders contained only `desktop.ini`. No runtime variable was set in the inspected process,
User or Machine scope. No SSH config/known_hosts was present; Pi access is not established.

| Checkout | Observed HEAD / state |
|---|---|
| `C:\Users\leepa\OneDrive\Documents\GitHub\project_elrond` | `e4f0243`, `codex/validation-campaign-159`, 11 dirty paths; preserved |
| `D:\GitHub\project_elrond` | `c817094a4bfd8aa68d1e41dd0cc0d8d8fe9eafc3`, `main`, clean; untouched |
| `C:\Users\leepa\elrond-runtime-recovery-190` | isolated recovery worktree, based on `2bdf6f323464e485a6705bd088d2b1a2742f34b2` |

Searches of `D:\`, the OneDrive tree and Documents found no Elrond DB, only unrelated
`Thumbs.db` files. `D:\Elrond` is empty. Checkout logs contained `.gitkeep` only. These are
bounded local findings, not proof that no store exists anywhere. The reusable command was also
exercised; its own uv/Python process chain is diagnostic activity, not a deployed coordinator.

The reusable scan examined 982 entries on `D:\`, 8,961 in OneDrive and 8 in Documents,
without hitting its 100,000-entry per-root cap. `D:\System Volume Information`,
`D:\WindowsApps` and legacy Documents library junctions were unreadable. Dependency/cache
directories were excluded. `quantbot preflight` independently reports missing paper credentials
for PAPER_TRADING/MARKET_DATA and missing model routing for RESEARCH; it does not probe a broker.

The Alienware inventory previously posted on #190 records a plausible schema-12 ledger at
`E:\Documents\GitHub\project_elrond\.claude\worktrees\quantbot-trading-system-2bbc25\quantbot.db`,
with account/reconciliation records through August 20 and last run HALTED on August 19.
That is **prior issue evidence, not a new remote measurement in this run**. It also reports a
disabled `QuantBotDaemon` task and a historically cleared kill switch. Do not infer current
broker readiness or clear anything from those historical rows. Global store/coordinator
authority and Pi state remain **UNKNOWN**; #190 remains open.

## Local verification — 2026-09-05

Executed in the isolated Astra worktree, Python 3.11.16 on Windows:

| Command | Result |
|---|---|
| `uv sync --frozen --group dev` | locked environment installed |
| `uv run ruff check src tests` | clean |
| `uv run mypy --platform linux src` | clean, 223 source files |
| `uv run mypy --platform win32 src` | clean, 223 source files |
| `uv run pytest` (final full run) | **2,653 passed, 18 skipped, 1 warning**, 377.88 seconds |
| `git diff --check` / staged equivalent | clean |
| `git status --short` | only the intended recovery changes before commit |

31 tests added. The 18 skips are 12 OS-sandbox/WSL cases, four unconfigured real model-server
cases and two Windows symlink cases. Native Linux/Pi execution, broker readiness, cross-host
retirement and reboot recovery were not tested. The warning is the existing Starlette/httpx
TestClient deprecation; dependencies were not changed.

An earlier full run reported one failure, 2,646 passed and 18 skipped: the new copied-lineage
fixture left a WAL connection open, correctly causing content inspection to refuse. Closing
the fixture before copying fixed the test; the final full run above includes that correction
and all six new supervisor restart checks. No operational WAL was altered to make a check pass.
Hosted Actions remain `workflow_dispatch` only, unchanged; no hosted run was requested.
