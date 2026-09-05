# Capture and compare recovery evidence (#190 / #220)

These commands inspect state. They never start Elrond, import its operational package, acquire
locks, migrate/create a database, submit orders or clear the kill switch. They write only the
tool extraction directory and redirected evidence files. Leave runtime checkouts, environments
and database files in place. Do not run setup scripts, `uv run`, `doctor` or the supervisor.

The portable command requires an **existing Python 3.11 or newer**, with no third-party packages.
`-I -S -B` excludes installed packages and prevents bytecode writes. If compatible Python is
unavailable, collect the OS metadata below and report the database evidence as UNKNOWN; do not
install/upgrade the host's runtime to make collection succeed.

## Reviewed tool bundle

Use `elrond-recovery-tools-190.tar` and the SHA-256 supplied with the #190 implementation update.
Transfer the archive through an existing trusted channel or removable storage. On each host,
verify its SHA-256 against that published value before extracting it. No SSH identity/key is
supplied or discovered by these instructions. A report also includes hashes of every collector
source file, so captures can be compared with the reviewed source. Hashes are integrity checks,
not authenticated statements of host identity or authority.

To reproduce the archive from the reviewed PR commit in a development checkout:

```powershell
git archive --format=tar --output="$env:USERPROFILE\elrond-recovery-tools-190.tar" HEAD scripts/recovery_evidence.py src/quantbot/operations/store_inventory.py src/quantbot/operations/checkout.py src/quantbot/operations/host_inventory.py src/quantbot/operations/recovery_compare.py src/quantbot/storage/target.py docs/runtime-evidence-handoff.md
Get-FileHash -Algorithm SHA256 -LiteralPath "$env:USERPROFILE\elrond-recovery-tools-190.tar"
```

Only run that packaging command from the reviewed commit; HEAD on an old host is not the tool
version to use. The archive contains source and this guide, never a database or credentials.

## Alienware: exact PowerShell capture

Place the verified archive in the Alienware user's Downloads directory. Use fresh output/tool
directories: `New-Item -ErrorAction Stop` intentionally refuses to overwrite earlier evidence.
Run in the existing operator account. The paths below come from the historical Alienware
inventory; absent paths are reported as absent, not replaced with new stores.

```powershell
$ErrorActionPreference = 'Stop'
$archive = Join-Path $env:USERPROFILE 'Downloads\elrond-recovery-tools-190.tar'
Get-FileHash -Algorithm SHA256 -LiteralPath $archive
# Compare with the SHA-256 published on #190 before continuing.
$toolRoot = Join-Path $env:USERPROFILE 'elrond-inventory-190'
$evidenceRoot = Join-Path $env:USERPROFILE 'elrond-evidence-190'
New-Item -ItemType Directory -Path $toolRoot -ErrorAction Stop | Out-Null
New-Item -ItemType Directory -Path $evidenceRoot -ErrorAction Stop | Out-Null
tar -xf $archive -C $toolRoot
if ($LASTEXITCODE -ne 0) { throw 'Tool archive extraction failed' }
py -3 --version
Set-Location -LiteralPath 'E:\Documents\GitHub\project_elrond'
$captureArgs = @(
  '-3', '-I', '-S', '-B', "$toolRoot\scripts\recovery_evidence.py", 'capture',
  '--repo', 'E:\Documents\GitHub\project_elrond',
  '--candidate', 'E:\Documents\GitHub\project_elrond\quantbot.db',
  '--candidate', 'E:\Documents\GitHub\project_elrond\.claude\worktrees\quantbot-trading-system-2bbc25\quantbot.db',
  '--candidate', 'E:\Documents\GitHub\project_elrond\.claude\worktrees\quantbot-trading-system-2bbc25\quantbot-backup-preV7-20260820.db',
  '--candidate', 'E:\Documents\GitHub\project_elrond\.claude\worktrees\quantbot-trading-system-2bbc25\quantbot-archived-v1.0.0-old-account.db',
  '--scan-root', 'E:\Documents\GitHub',
  '--scan-root', 'C:\Users\JHutc\Documents\Codex',
  '--scan-root', 'D:\',
  '--scan-root', $env:USERPROFILE
)
& py @captureArgs 2> "$evidenceRoot\alienware.stderr.txt" |
  Set-Content -LiteralPath "$evidenceRoot\alienware.json" -Encoding utf8
$captureExit = $LASTEXITCODE
if ($captureExit -ne 1) { throw "Capture failed: exit $captureExit; preserve stderr" }
$report = Get-Content -LiteralPath "$evidenceRoot\alienware.json" -Raw | ConvertFrom-Json
$report | Select-Object report_version, as_of, host, store_authority, candidate_errors
$report.os_inventory | ConvertTo-Json -Depth 12
$report.candidate_scans | Select-Object root, entries_seen, limit_reached, unreadable
Get-FileHash -Algorithm SHA256 -LiteralPath "$evidenceRoot\alienware.json"
```

If `py` is unavailable, substitute the full path of an already installed Python 3.11+ executable
and omit `-3`. Do not activate/synchronize an old trading environment. If the historical root
checkout is absent, use a real existing working directory and retain its absent path as `--repo`.
`--repo` only inspects Git; it does not change environment or resolve relative DB paths for you.

Windows capture reports visible Python/Elrond processes, task state/enablement/executables/working
directories/triggers, matching services, Run/RunOnce entry names, Startup filenames, persisted
runtime path values and registered WSL distributions. Task matching uses actual action fields,
not formatted/truncated PowerShell output. Review generic wrappers/shortcuts and inaccessible
areas privately; no matching name does not prove they cannot launch Elrond. Disabled
`\QuantBotDaemon` remains a retirement item. Do not re-enable it to inspect it.

For additional paths found in persisted configuration or scans, rerun with additional
`--candidate`/`--scan-root` arguments and a **new output filename**. Keep the original capture.
Do not print credential files, environment dumps, raw task arguments or command lines.

## Pi: exact commands at the physical console or over already trusted access

**PI_STATE=UNKNOWN from Omen until actual Pi evidence is returned.** No address is guessed and
no unknown SSH key accepted. Place the verified archive at `$HOME/elrond-recovery-tools-190.tar`.
These commands run **on the Pi**, not against an assumed remote address:

```sh
set -eu
archive="$HOME/elrond-recovery-tools-190.tar"
sha256sum "$archive"
# Compare with the SHA-256 published on #190 before continuing.
tool_root="$HOME/elrond-inventory-190"
evidence_root="$HOME/elrond-evidence-190"
mkdir "$tool_root" "$evidence_root"
tar -xf "$archive" -C "$tool_root"
{
  hostname
  uname -a
  date -u '+%Y-%m-%dT%H:%M:%SZ'
  ps -eo pid,ppid,lstart,comm
  systemctl list-unit-files --no-pager --no-legend || true
  systemctl --user list-unit-files --no-pager --no-legend || true
  systemctl show quantbot.service dashboard.service control.service \
    dashboard-refresh.service dashboard-refresh.timer \
    -p Id -p ActiveState -p SubState -p UnitFileState -p MainPID \
    -p FragmentPath -p DropInPaths -p WorkingDirectory -p EnvironmentFiles || true
} > "$evidence_root/pi-os.txt" 2> "$evidence_root/pi-os.stderr.txt"
python3 --version
if python3 -I -S -B -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  capture_exit=0
  python3 -I -S -B "$tool_root/scripts/recovery_evidence.py" capture \
    --repo /home/jfhutchi/quantbot \
    --candidate /home/jfhutchi/quantbot-data/quantbot.db \
    --scan-root /home --scan-root /opt --scan-root /var/lib --scan-root /srv \
    > "$evidence_root/pi.json" 2> "$evidence_root/pi.stderr.txt" || capture_exit=$?
  test "$capture_exit" -eq 1
  sha256sum "$evidence_root/pi.json"
else
  printf '%s\n' 'PI_DATABASE_EVIDENCE=UNKNOWN: Python 3.11+ unavailable' \
    > "$evidence_root/pi-database-unknown.txt"
fi
```

The repo/store paths are **historical deployment paths in `deploy/pi`**, not claims they exist.
Inspect actual `WorkingDirectory` and `EnvironmentFiles` locations from metadata. Inspect only
non-secret path settings privately; do not source an environment file or print its contents.
Rerun against other measured candidates/mounts with new filenames. Preserve errors, scan caps
and excluded paths. If an already-authorized privileged operator captures another user's
scope, keep that as a separate capture; this command does not acquire privilege automatically.

The portable Linux collector reads visible processes, named system/user units, a bounded scan
of systemd/cron/autostart/profile files and current-user crontab. It reports file hashes and
reference matches, never their contents. Other users, indirect wrappers, containers, remote
schedulers and unreadable files remain UNKNOWN. A current-user service list alone cannot
retire other users' enabled/lingering services.

## Offline comparison on Omen

Return `alienware.json` / `pi.json`, their hashes, and the Pi OS metadata/errors through a trusted
channel. Keep original captures private and unchanged. Compare available reports only; never
fabricate an empty Pi report to eliminate missing-host warnings. From the recovery checkout:

```powershell
$python = 'C:\Users\leepa\elrond-runtime-recovery-190\.venv\Scripts\python.exe'
$script = 'C:\Users\leepa\elrond-runtime-recovery-190\scripts\recovery_evidence.py'
$evidenceRoot = Join-Path $env:USERPROFILE 'elrond-evidence-190'
& $python -I -S -B $script compare --expect omen --expect alienware --expect pi `
  --report "omen=$evidenceRoot\omen.json" --report "alienware=$evidenceRoot\alienware.json" `
  --report "pi=$evidenceRoot\pi.json" --format markdown |
  Set-Content -LiteralPath "$evidenceRoot\comparison.md" -Encoding utf8
& $python -I -S -B $script compare --expect omen --expect alienware --expect pi `
  --report "omen=$evidenceRoot\omen.json" --report "alienware=$evidenceRoot\alienware.json" `
  --report "pi=$evidenceRoot\pi.json" --format json |
  Set-Content -LiteralPath "$evidenceRoot\comparison.json" -Encoding utf8
```

Omit `--report "pi=..."` if unavailable; retain `--expect pi`. Capture/compare exit **1** means
valid evidence with authority/startup unverified. Compare exit **2** means invalid/unreadable
input; do not use its output as a successful comparison. Save each subsequent comparison under
a fresh name. The comparator only opens JSON reports, never paths named inside them.

Inputs are limited to 32 MiB per report and 200 plausible candidates per comparison; split
oversized captures explicitly and retain all resulting reports/coverage.

The Markdown contains per-candidate evidence and pairwise reasons automatic choice is unsafe.
The JSON contains the same values. Report order does not change the result. File identity is
scoped to one capture; same UUID/hostname/file ID across two captures never collapses hosts.
Matching hard-link aliases within one capture require matching file IDs **and** content hashes.

SQLite base-file hashes detect differences outside bounded latest-row summaries, but a different
hash can also result from storage layout. Hashing is capped at 512 MiB and SQLite queries have
a progress budget; unavailable evidence stays UNKNOWN. Account snapshot hashes cover the
recorded snapshot ID/run/account/time/cash/buying power/equity/currency, not all position history.
History counts cover runs/orders/fills/reconciliation/research/ingestion, not their legitimacy.
Runtime SHA is taken from recorded runtime rows where present, never substituted from strategy
deployment SHA. Ingestion sources/cursor hashes and raw timestamps preserve LIVE/BACKFILL
distinctions; portable capture does not reproduce #198's computed freshness/status evaluation.
Use existing `deployment-status` for that evaluation once a compatible environment is available.

Nonempty WAL/journal files refuse inspection and hashing. Do not remove them, force a checkpoint
or copy just the base database. Consistent snapshot acquisition is a separate, explicitly scoped
recovery step with source/sidecar provenance; this task does not perform it. Matching bytes,
SUCCEEDED rows or qualification dates never establish a legitimate forward day automatically.

## Minimum evidence for authority and sole PAPER coordination

1. Fresh, traceable inventory of every historically used host and relevant storage scope,
   including actual runtime selectors, processes and resurrection mechanisms. Resolve scan
   exclusions and unreadable candidates relevant to Elrond. An absent Omen store proves nothing
   about Alienware/Pi authority.
2. A consistent candidate snapshot and physical/source provenance; schema/revision/UUID where
   present; account hash and snapshot lineage; run/order/fill/reconciliation history; strategy
   configuration/deployment identity; market/qualification/ingestion dates; recorded runtime SHA.
   Legacy schemas may lack UUID/runtime metadata: document the absence and corroborate with
   source provenance and historical records, never invent replacement observations.
3. Independent corroboration that the selected history belongs to the intended **PAPER** account
   and legitimate runs: verify account/order/fill/position history and reconciliation against the
   actual paper broker records in a later authorized read-only readiness step. Resolve discrepancies
   and explain every competing candidate, including old-account archives and research-only stores.
   Latest rows/counts/hash equality alone cannot prove complete or legitimate history.
4. If candidates diverge, retain **AMBIGUOUS** until an explicit, evidence-backed operator recovery
   decision documents the selection and each alternative's disposition. Preserve the source
   evidence; never union ledgers by intuition. Record what remains missing and why it does not
   invalidate the chosen lineage. The tooling never emits an authoritative winner.

Before Omen becomes the sole coordinator, verify retirement of Alienware's `\QuantBotDaemon`
task (including logon/retry paths), old watchdog/supervisor copies and Startup/Run entries;
Pi system/user coordinator services/timers/cron and any mutating `control.service` or dashboard
refresh wrappers; other users' startup/linger, containers, collectors with write access and remote
schedulers. A watchdog role must have no path to start a second coordinator or write the trading
store/account. Preserve logs/state and the startup configuration for audit. Remove obsolete
launch/write/credential capabilities through a separately reviewed retirement action, then
measure the result; source-code guards do not retire old installed copies.

Only after store authority and retirement are proved: preserve a consistent backup, rehearse
the existing migrations on that backup, deliberately migrate the selected operational store,
pin reviewed code/config/locked dependencies, verify PAPER account/readiness/reconciliation and
kill-switch underlying conditions, then deliberately test single-coordinator startup/reboot.
Omen currently has no WSL installation; the intended deployment environment remains a measured
prerequisite, not an installed topology. No action in this guide authorizes startup or kill clearing.
