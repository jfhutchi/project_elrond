# Native Windows PAPER runtime on the HP Omen

The HP Omen laptop is the sole permitted Elrond coordinator. The Raspberry Pi is retired by
operator policy and must not be contacted or used as a coordinator, watchdog, dashboard or worker.
The Omen runtime has no Alienware or other-computer dependency.

This directory's current entry point is `setup-windows.ps1`. The older WSL/systemd files remain as
historical deployment artifacts and are not compatible with the measured current host: WSL is not
installed and Docker is absent.

## Measured host and deployment status — 2026-09-08

The target is an HP OMEN by HP Gaming Laptop 16-n0xxx running Windows 11, with an AMD Ryzen 7 6800H
(8 cores / 16 logical processors), approximately 31.2 GiB RAM, an AMD Radeon RX 6650M (8 GB VRAM
expected from the model), approximately 781 GiB free on C: and 931 GiB free on D:. Python 3.11.16
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

Run from an ordinary PowerShell session at the reviewed checkout:

```powershell
& .\deploy\omen\setup-windows.ps1
```

The installer must keep `TRADING_MODE=PAPER`, `BROKER_ENVIRONMENT=PAPER`, and
`LIVE_TRADING_ACKNOWLEDGED=false`; use absolute paths outside the checkout for mutable state,
locks, reports and logs. It registers the current-user Interactive task that this account can
actually create. Requiring elevation for an AtBoot or S4U task is a separate operational change,
not something this installer may silently approximate.

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
