# Runtime topology

Which machine runs what, and why the Raspberry Pi Zero W is no longer a candidate for the
control plane (#38).

## The constraint that drives this

The v0.1 system ran on a Pi Zero W and that was fine, because v0.1 was a paper-trading daemon
and a ledger. v0.2 adds Kronos inference, semantic analysis over a local model runtime, a
research queue and a growing durable store. **The fact that v0.1 fits does not mean v0.2 can be
colocated there**, and the failure mode is not a slow research job — it is a research job
thrashing a 512MB host onto a microSD and taking the broker, risk engine and reconciliation down
with it.

So the rule is enforced in code rather than written down as guidance. See
[`quantbot.research.placement`](../src/quantbot/research/placement.py).

## Measured hosts

Taken with `HostProfile.measure()` on each machine on 2026-08-22, not copied from a spec sheet.

| Host | Architecture | Cores | RAM | GPU VRAM | Storage |
|---|---|---|---|---|---|
| `omen-windows` (HP Omen, Windows 11) | amd64 | 32 | 32,486 MB | 16,376 MB | C: 930 G (457 G free), E: 931 G (210 G free) |
| `omen-wsl2` (WSL2 Ubuntu on the same machine) | x86_64 | 32 | 15,847 MB | 16,376 MB | 1,007 G ext4 (936 G free) |
| `pi-zero-w` | armv6l | 1 | ~434 MB usable | none | microSD |

Two things this measurement settled that the issue had assumed otherwise:

- **The Omen has a 16 GB GPU of its own.** The issue treats the RTX desktop as the only GPU
  host. It is not — Kronos and local model inference can run on the coordinator without the
  desktop being powered on, which removes the availability dependency the issue was worried
  about. The desktop remains preferable for anything that needs more than 16 GB of VRAM.
- **WSL2 sees half the host's RAM by default.** 15,847 MB against the Windows side's 32,486 MB.
  That is a `.wslconfig` default, not a hardware limit, and it matters because placement reads
  what the kernel reports rather than what the box contains. Raising it is an operator decision;
  until then, jobs are sized against 15 GB.

The Pi's figures are its published specification. It is the one host here that could not run the
measurement: it answers on both service ports, but this session had no working SSH credential
for it, so its numbers are stated rather than measured and are marked as such.

## Roles

**Coordinator — `omen-wsl2`.** Scheduler and supervisor, durable state, hypothesis registry and
research lifecycle, broker and paper execution, risk engine, reconciliation, kill switch,
research queue, worker dispatch. Elrond lives inside WSL2 rather than on Windows directly, so
the runtime is a Linux process tree with Linux paths and Linux systemd semantics.

**Compute — `omen-wsl2` now, RTX desktop when it is on.** Kronos inference, local LLM inference,
larger backtests, any fine-tuning. Dispatched through the worker interface, never inside the
trading daemon process.

**Watchdog — `pi-zero-w`.** Heartbeat freshness, endpoint checks, an alert trigger. It hosts the
dashboard and the control surface, and **nothing the trading path depends on**. The Pi being
switched off is an inconvenience, not an outage.

## How the rule is enforced

`place(job, hosts)` returns a host that satisfies the job or raises `NoCapableHost` naming what
each host failed. It never returns a least-bad option: degrading a Kronos job onto the Pi is the
outage this exists to prevent, and a refused job is recoverable in a way a downed control plane
is not.

Three properties are load-bearing:

- **A host profile must be measured, not declared.** `HostProfile.measure()` reads the machine
  and stamps a module-private token; a profile constructed any other way is `DECLARED` and
  cannot receive work, even if it sets `provenance=MEASURED` explicitly. This is the recurring
  defect class in this project — a gate reading its input from the thing it constrains — and an
  undersized host with a copied config is exactly the case that would exploit it.
- **Unknown blocks.** A capacity that could not be read is treated as insufficient, and `None`
  is kept distinct from `0`: a host with no GPU has been measured, a host that was never probed
  has not, and neither is adequate for a job that needs one.
- **The smallest sufficient host wins**, so the GPU box stays free for work that needs it rather
  than being taken by the first job to ask.

The declared requirements live beside the code that uses them: `KRONOS_INFERENCE` (2 cores,
2 GB, x86_64 or ARM64 — the 2 GB floor comes from the 660 MB peak measured in #36 plus room for
the interpreter and the input snapshot), `SEMANTIC_ANALYSIS` (2 cores, 1 GB, and a local model
endpoint), `CONTROL_PLANE` (1 core, 512 MB) and `WATCHDOG` (1 core, 128 MB).

The Pi is not banned by name. A rule naming a host would be wrong the moment the hardware
changed. It is sized out: 434 MB does not meet the control plane's 512 MB floor, and does meet
the watchdog's.

## Not done yet

- **Automatic recovery after a Windows reboot or WSL restart** is not implemented. The Pi's
  services recover through systemd; the coordinator's do not yet, because the coordinator is not
  yet the thing running the daemon.
- **The move itself has not happened.** This records the topology, the measurements and the
  enforcement. Elrond's durable state still lives where it lived, and migrating it is a separate
  operator-visible step that should not happen silently.
- **The move itself is the remaining step.** Placement now runs on dispatch: a `SubprocessWorker`
  given `requirements=` checks this host *before* spawning the subprocess and raises
  `NoCapableHost` rather than starting work the machine cannot carry. It raises rather than
  returning a failure deliberately -- a caller retrying a broken worker is doing the right
  thing, and a caller retrying an incapable host is not, because nothing ran and running it
  again here will not change that. A worker declaring no requirements is not gated, so existing
  workers are unaffected.
