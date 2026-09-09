# Runtime topology

## Current policy and observed state — 2026-09-08

The HP Omen laptop is the sole permitted Elrond coordinator. It carries the scheduler,
supervision, durable PAPER ledger, broker integration, risk engine, reconciliation, kill switch,
dashboard and local workers. None of these roles depends on the Raspberry Pi, Alienware desktop,
or another computer.

The Raspberry Pi is **retired entirely by operator policy**. Do not contact it and do not assign it
coordinator, watchdog, dashboard or worker duties. This is a topology decision, not a remote
measurement: its current power, process, startup and storage state is deliberately `NOT_PROBED`.
The retired installer in [`deploy/pi/setup.sh`](../deploy/pi/setup.sh) exits before inspecting or
changing the host.

The policy designates Omen as the only authority allowed to run, but it does not manufacture proof
that Omen currently holds global authority. Cross-host fencing remains `UNKNOWN`; a local lock does
not fence an unmeasured stale installation on some other host. PAPER broker reconciliation has
been exercised and is clean. The tested entry-budget fix at
`094fefc1538e46dfca1b48c2e0a1ca50857fc43e` is the active clean runtime commit after the successful
deployment retry. At 00:10 UTC September 9 the legitimate clearance command remeasured readiness
and cleared the switch. Subsequent doctor/reconciliation checks passed. The prior ERROR run is
preserved; no rejected intent was replayed. See [current measured evidence](omen-runtime-2026-09-09.md).

## Measured Omen capacity — 2026-09-08

| Property | Observation |
|---|---|
| Host | HP OMEN by HP Gaming Laptop 16-n0xxx, Windows 11 |
| CPU | AMD Ryzen 7 6800H, 8 cores / 16 logical processors |
| RAM | approximately 31.2 GiB usable |
| GPU | AMD Radeon RX 6650M; 8 GB VRAM expected from the device model, not runtime-probed here |
| Storage | C: approximately 781 GiB free; D: approximately 931 GiB free |
| Runtime | native Windows, Python 3.11.16 and `uv` |
| WSL | not installed |
| Docker | absent |

No Elrond process, startup registration, operational ledger or credentials were discovered in the
bounded pre-install inventory. An expanded old-profile scan found only pytest fixture databases,
not an operational candidate. The initial store at `D:\Elrond\state\quantbot-paper.db` is preserved.
After the operator supplied a fresh PAPER account, a separate schema-27 store was established at
`D:\Elrond\state\quantbot-paper-fresh-20260908.db`, UUID `cd2a5911-853a-484e-bbc8-d0c27b5d27aa`.
Its account was reconciled before activation. Prior forward history was not migrated; the seeded
institutional findings are not trading history. A verified online backup retains its identity.

All seven tasks now use local standard account `LEANNES-LAPTOP\erlond`, Password logon, Limited
privileges, AtStartup and timed triggers. Console, PAPER daemon, protection and model server run
in noninteractive session 0; ingestion, research and reporting complete between scheduled runs.
Task Scheduler stop/start removed the console process tree and restored HTTP 200 against the
same store. Startup registration is verified; a physical reboot has not been tested. Earlier
Interactive registration and elevation failures are historical, superseded by this account setup.

Portable Ollama 0.33.3 is registered as `ElrondModelServer` and measured listening only on
`127.0.0.1:11434` in CPU mode. Two offline Kronos runs produced the same 240 values and peaked near
480 MB; that demonstrates local worker execution, not broker state or forward evidence. See the
[dated Omen runtime report](omen-runtime-2026-09-08.md) for the evidence record.

## Role boundaries

**Required Omen roles.** Coordinator, supervisor, dashboard, storage, paper execution, risk,
reconciliation and kill-switch enforcement run natively on Windows. Required runtime health must
be derivable from this one host.

**Optional Omen work.** Kronos inference, semantic analysis and research jobs may run as bounded
workers on Omen when measured capacity satisfies their requirements. A worker never receives
coordinator, broker, risk, promotion or kill-switch authority.

**Future workers.** Another computer may later add compute capacity through the worker interface.
It is optional by construction and cannot become a runtime dependency merely by being reachable.

## Placement enforcement

[`quantbot.placement`](../src/quantbot/placement.py) uses measured `HostProfile` capacity and
refuses a job when no capable host exists. It never returns a least-bad candidate. Unknown capacity
blocks placement, and a profile declared by a caller cannot impersonate a measurement.

The current requirement objects are `KRONOS_INFERENCE` (2 cores, 2 GB and a supported 64-bit
architecture), `SEMANTIC_ANALYSIS` (2 cores, 1 GB and a current local model endpoint),
`CONTROL_PLANE` (1 core, 512 MB), and `WATCHDOG` (1 core, 128 MB). `WATCHDOG` is retained as a
generic capability for an optional future host; no current machine is assigned that role.

A `SubprocessWorker` with requirements checks the executing host before spawning. This preserves
the historical protection added after a 512 MB Pi Zero W was found incapable of carrying research
jobs alongside the control plane. The Pi's later retirement is a policy boundary in addition to
that capacity boundary.

## Historical topology evidence — retained, not current

The following measurements and conclusions were recorded on 2026-08-22. The labels conflated an
earlier HP Omen/desktop environment with the current laptop, and the table must not be used as a
current deployment inventory.

| Historical host label | Architecture | Cores | RAM | GPU VRAM | Storage |
|---|---|---|---|---|---|
| `omen-windows` | amd64 | 32 | 32,486 MB | 16,376 MB | C: 930 G (457 G free), E: 931 G (210 G free) |
| `omen-wsl2` | x86_64 | 32 | 15,847 MB | 16,376 MB | 1,007 G ext4 (936 G free) |
| `pi-zero-w` | armv6l | 1 | ~434 MB usable | none | microSD |

That work proposed `omen-wsl2` as coordinator and the Pi as a lightweight watchdog/dashboard. It
also established useful general facts: WSL could expose less RAM than its Windows host; heavy jobs
must be gated on measured capacity; and the Pi Zero W did not meet the 512 MB control-plane floor.
Those facts remain evidence about the earlier machines and the placement design. The proposed role
assignment was superseded by the 2026-09-08 native Windows topology and Pi retirement policy.
