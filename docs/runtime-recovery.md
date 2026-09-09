# Recover runtime truth before deployment (#190)

Use `deployment-status` before `doctor`, the ordinary supervisor, any installer, or any
database command. `doctor` probes the broker and builds the runtime; construction can persist
deployment/policy metadata. The ordinary supervisor can restart a daemon. Neither is a
read-only inventory tool.

Current topology policy, declared by the operator on 2026-09-08, retires the Pi entirely and
forbids contacting it. The native Windows Omen is the sole permitted PAPER coordinator and has no
Alienware dependency. This policy removes Pi state as a startup prerequisite; it does not prove
global coordinator/store authority, broker state, or fencing of every other historical host.
Global fencing remains `UNKNOWN` to recovery inventory. PAPER broker reconciliation has since
been measured and is clean. The entry-budget fix is deployed and the switch cleared after
measured readiness. See the current snapshot below for the selected store and verification limits.

The [Alienware/Pi evidence handoff](runtime-evidence-handoff.md) is preserved as a historical
recovery artifact. Do not execute its Pi access procedure under the current policy.

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
local schedulers sharing its configured path. Neither fences a stale Windows checkout or another
unmeasured host using a different lock/store against the same broker account. Existing lock files contain a NUL
byte, not owner PID/start metadata. Inventory deliberately reports owner/held as `UNKNOWN`
rather than acquiring the lock (which can create it) or claiming a PID owns it from coincidence.

**This change does not solve cross-host fencing or mechanically retire old installations.** The
Pi is excluded by operator policy and the repository installer refuses it, but no remote probe was
performed. Older supervisors or Task Scheduler entries on other hosts may still start old code.
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

## Pi retirement policy

The Pi is `RETIRED` with authority `OPERATOR_DECLARED`; its remote state is `NOT_PROBED`. Do not
contact it, guess an address, accept a host key, install credentials, or run recovery commands on
it. `deployment-status` reports this policy separately from remote evidence and no longer emits
`PI_STATE_UNKNOWN` as a startup blocker. [`deploy/pi/setup.sh`](../deploy/pi/setup.sh) returns a
named refusal without making changes.

This declaration must not be generalized into proof that every historical host is fenced. Keep
`GLOBAL_COORDINATOR_AUTHORITY_UNVERIFIED` and `OTHER_HOST_RETIREMENT_UNVERIFIED` until the remaining
authority evidence exists.

## Measured Omen snapshot — 2026-09-08

The current target is an HP OMEN by HP Gaming Laptop 16-n0xxx running Windows 11, with an AMD Ryzen
7 6800H (8 cores / 16 logical processors), approximately 31.2 GiB RAM, an AMD Radeon RX 6650M
(8 GB VRAM expected from the device model), approximately 781 GiB free on C: and 931 GiB free on
D:. WSL is not installed and Docker is absent. The native toolchain is Python 3.11.16 with `uv`.

The bounded pre-install searches found no Elrond process, startup entry, operational ledger or
credentials. An expanded `Windows.old\Users` scan found 774 schema-12 databases, all under
`pytest-of-leepa` paths and consistent with repository deployment fixtures; none was treated as an
operational candidate.

A fresh initial PAPER store was created at `D:\Elrond\state\quantbot-paper.db`, schema 27 / revision
0027, lineage `2244c782-29e9-4ab0-bff8-bced024a226b`, with 25 seeded historical findings and no forward
history. Its initial backup passed integrity checking and matched its logical dump. That store is
preserved; it is **not the active account ledger**.

The operator subsequently supplied a fresh PAPER account. Its active store is
`D:\Elrond\state\quantbot-paper-fresh-20260908.db`, schema 27 / revision 0027, UUID
`cd2a5911-853a-484e-bbc8-d0c27b5d27aa`. Reconciliation preceded activation; no missing forward
history was reconstructed. The verified online backup at
`D:\Elrond\backups\quantbot-paper-local-account-20260908.db` retains this UUID and the nine accepted
and one rejected intents. At 23:00 UTC PAPER reconciliation reports zero differences and no
unresolved intents. The subsequent deployment and measured clearance at 00:10 UTC September 9
restored readiness without replaying the rejected intent or rewriting the ERROR run.

All seven tasks now use the local `erlond` account with Password logon, Limited privileges and
AtStartup plus timed triggers. Services run in session 0 and a Task Scheduler console stop/start
restored the same store and HTTP service. A physical reboot has not been performed. The active
commit is `094fefc1538e46dfca1b48c2e0a1ca50857fc43e`. The latest verified online backup is
`D:\Elrond\backups\quantbot-paper-entry-budget-20260909.db`, retaining the same store identity.
The full evidence record is [the Omen runtime report](omen-runtime-2026-09-09.md).

## Historical Omen snapshot — 2026-09-05

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
broker readiness or clear anything from those historical rows. Global store/coordinator authority
remained **UNKNOWN** in that snapshot; #190 remained open. Current Pi retirement is a later
operator policy, not a reinterpretation of this historical evidence.

## Local verification: 2026-09-05

Follow-up review of #220 against main `2bdf6f323464e485a6705bd088d2b1a2742f34b2`,
in the isolated Astra worktree, existing locked environment, Python 3.11.16 on Windows:

| Command | Result |
|---|---|
| `.venv/Scripts/ruff.exe check src tests scripts/recovery_evidence.py` | clean |
| `.venv/Scripts/mypy.exe --platform linux src` | clean, 225 source files |
| `.venv/Scripts/mypy.exe --platform win32 src` | clean, 225 source files |
| `.venv/Scripts/pytest.exe` | **2,665 passed, 18 skipped, 1 warning**, 380.95 seconds |
| `git diff --check` / staged equivalent | clean |

The follow-up adds 12 tests (43 total across this PR), including real SQLite divergence hidden
behind identical latest dates/account IDs, byte preservation with site packages disabled,
sidecar refusal, cross-capture file-ID collisions, offline report-only comparison, timestamp
ordering, research-only candidates and private startup-wrapper inspection. Earlier tests of
current-schema readers, startup guards and runtime identity remain in the full passing suite.

The 18 skips are 12 OS-sandbox/WSL cases, four unconfigured real model-server cases and two
Windows symlink cases. Native Linux/Pi execution, broker readiness, cross-host retirement and
reboot recovery were not exercised. The warning is the existing Starlette/httpx TestClient
deprecation. No dependencies, ingestion control-plane schema, operational database or workflow
were changed. Hosted Actions remain manual-only; no hosted run was requested.
