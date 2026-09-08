# Native Windows PAPER runtime on the HP Omen

The HP Omen laptop is the sole permitted Elrond coordinator. The Raspberry Pi is retired by
operator policy and must not be contacted or used as a coordinator, watchdog, dashboard or worker.
The Omen runtime has no Alienware or other-computer dependency.

This directory's current entry point is `setup-windows.ps1`. The older WSL/systemd files remain as
historical deployment artifacts and are not compatible with the measured current host: WSL is not
installed and Docker is absent.

## Measured host and deployment status — 2026-09-08

**Latest activation:** The operator selected a separate, verified empty PAPER account. Its ledger
is `D:\Elrond\state\quantbot-paper-fresh-20260908.db`. Reconciliation passes and the normal
measured-readiness path cleared the kill switch. All six application tasks are enabled; console,
PAPER daemon and protection are running, and scheduled maintenance has succeeded. See the
[current evidence follow-up](../../docs/omen-runtime-2026-09-08.md) for remaining freshness and
startup limitations. The original ledger and older inventory below are preserved history.

The target is an HP OMEN by HP Gaming Laptop 16-n0xxx running Windows 11, with an AMD Ryzen 7 6800H
(8 cores / 16 logical processors), approximately 31.2 GiB RAM, an AMD Radeon RX 6650M (disabled, device error 22; CPU execution verified), approximately 781 GiB free on C: and 931 GiB free on D:. Python 3.11.16
and `uv` are available natively.

The bounded pre-install inventory found no Elrond process, startup registration, operational ledger
or credentials. A later expanded scan found 774 old schema-12 databases under pytest temporary
paths and classified all of them as test fixtures, not operational candidates. Task Scheduler
registration was exercised with an Interactive logon for the current user. AtBoot and S4U
registration returned Access denied under the non-administrator account. Therefore the supported
configuration supervises Elrond while the user is signed in; it does not claim unattended reboot
recovery.

Deployment status remains `PARTIAL` until the trading task, launched processes and broker
reconciliation are measured. Omen is the only host policy permits to hold
authority, but global coordinator/store authority and other-host fencing remain `UNKNOWN`. Broker
state is unprobed. Do not clear the kill switch or claim forward qualification from installation.

## Install

The runtime Python is installed at `D:\Elrond\tools\python`, outside packaged-app AppData
virtualization. When preparing this host from packaged Codex, use an explicit external install:

```powershell
uv python install 3.11.16 --install-dir D:\Elrond\tools\python --no-bin
uv venv --python D:\Elrond\tools\python\cpython-3.11.16-windows-x86_64-none\python.exe
uv sync --locked
```

Do not replace an existing environment without first stopping its tasks and preserving needed
configuration. A Python path visible inside Codex does not prove Task Scheduler can see it.
Run from an ordinary PowerShell session at the reviewed checkout:

```powershell
& .\deploy\omen\setup-windows.ps1 `
    -EnvironmentFile 'C:\Users\leepa\.config\quantbot\quantbot.env' `
    -Database 'D:\Elrond\state\quantbot-paper-fresh-20260908.db'
```

The installer must keep `TRADING_MODE=PAPER`, `BROKER_ENVIRONMENT=PAPER`, and
`LIVE_TRADING_ACKNOWLEDGED=false`; use absolute paths outside the checkout for mutable state,
locks, reports and logs. It registers the current-user Interactive task that this account can
actually create. Requiring elevation for an AtBoot or S4U task is a separate operational change,
not something this installer may silently approximate.

The checkout must be committed and clean before installation. Six separate tasks are registered:
`Elrond Console`, `Elrond Daemon`, `Elrond Protection`, `Elrond Ingestion`, `Elrond Research`, and
`Elrond Report`. Broker-dependent tasks remain disabled when PAPER credentials are missing.
Start enabled tasks with `Start-ScheduledTask`; scheduled logon and retry triggers also apply.
Before updating a running deployment, stop its tasks through Task Scheduler, verify shutdown,
then rerun with `-UpdateManaged` to pin the new clean commit and start enabled tasks again.
The installer refuses to update running tasks because registration alone does not replace them.

Scheduled actions run through a small Windows GUI executable so Windows does not allocate a
terminal before PowerShell can hide it. `build-service-host.ps1` compiles the reviewed C# sources
with the installed .NET Framework compiler into `service-host/<source fingerprint>/` beside the
database, outside Git. The installer checks a cached executable's digest before reuse. The host
owns a kill-on-close Job Object before launching PowerShell with `CreateNoWindow`; task shutdown
therefore stops the complete descendant tree. Existing wrapper validation and nested jobs remain
in place. Startup errors go to `launcher-logs/<component>.log` beside the database; normal
application logs retain their configured location. Native exit codes become task results.

The separately registered `ElrondModelServer` uses the same host with its existing
`start-model-server.ps1` arguments and an absolute launcher log as the first host argument.
Updating its action preserves its task triggers, principal and settings; stop it and verify
descendant shutdown before replacing the action, then start it again.

The smallest broker activation step is to supply `ALPACA_PAPER_API_KEY`, `ALPACA_PAPER_API_SECRET`
and the verified `EXPECTED_ACCOUNT_ID` in the existing restricted external environment file.
Do not put credential values in shell commands, chat, Git or task arguments. After injection,
measure PAPER readiness and reconcile real orders/fills/positions through the normal CLI.
Keep the kill switch engaged until its legitimate measured-clearance path permits otherwise.
An elevated operator may use `-Mode Boot -Credential (Get-Credential)` for password-backed startup
registration; this account cannot complete that step. No automatic Windows login is configured.

After registration, establish runtime truth with read-only inventory before commands that build a
writable runtime:

```powershell
uv run quantbot deployment-status --inventory
uv run quantbot deployment-status --startup-check
```

Then inspect the scheduled task, process ownership, configured absolute paths and logs. Broker
credentials, if later supplied outside the repository, permit a read-only reconciliation check;
their absence is an honest refusal, not a reason to populate placeholders.

## Required local roles

The native Windows runtime supervises the coordinator, position-protection process and dashboard
on Omen. Research and inference workers are optional local subprocesses dispatched only when
measured capacity permits. No required heartbeat, recovery, alerting or control path may depend on
another host.

## Ledger provenance

No prior operational Elrond ledger was discovered in the bounded 2026-09-08 recovery scans. A fresh
PAPER ledger now exists at `D:\Elrond\state\quantbot-paper.db`, schema 27 / revision 0027, lineage
`2244c782-29e9-4ab0-bff8-bced024a226b`. It contains 25 seeded historical findings, zero runs,
fills or qualification observations, and the default engaged kill switch. Its initial backup at
`D:\Elrond\backups\quantbot-paper-initial-20260908.db` passed integrity checking and has an
identical logical dump. It does not inherit Alienware/Pi forward history.

Portable Ollama 0.33.3 currently runs through the `ElrondModelServer` task on CPU and listens only
on `127.0.0.1:11434`. Two deterministic offline Kronos runs completed with 240 values and an
observed peak near 480 MB. These checks establish local worker capability only, not trading or
forward evidence. The detailed record is [the Omen runtime report](../../docs/omen-runtime-2026-09-08.md).

## Historical WSL deployment artifacts

`setup.sh` and the systemd units were previously designed for a different Omen/WSL environment.
Earlier evidence reported WSL2 systemd, user lingering, 32 logical processors, 32,486 MB Windows
RAM, 15,847 MB guest RAM and a 16,376 MB GPU. Those observations remain useful history, but they do
not describe this laptop and do not prove anything is currently deployed. Do not run the WSL
installer on the measured native Windows target.
