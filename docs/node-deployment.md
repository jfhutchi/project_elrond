# Multi-node deployment

How a second machine becomes an Elrond node, and what has to exist before that is routine
rather than a bespoke project each time. Extends [`runtime-topology.md`](runtime-topology.md)
and #38.

## Status

`quantbot.node` exists: identity, capability detection, role policy. Everything else in the
backlog below is unbuilt. Nothing here is installed on any machine.

## What the repository actually assumes today

Recorded because three of these contradict what a multi-node installer would naturally assume,
and building against the assumption rather than the code would have produced a plausible
installer for a system that does not exist.

**One machine owns everything.** `Settings` reads process environment variables only
(`env_file=None`). The ledger is SQLite behind a single-writer lock (`storage/lock.py`). The
default paths in `.env.example` are *relative* — `quantbot.db`, `reports`, `quantbot.lock` —
so durable state follows whichever directory the process started in.

**Nothing is containerised.** There is no Dockerfile, no compose file, and no container
reference anywhere in the tree. The dependency set is SQLAlchemy, Alembic, httpx, pydantic,
structlog, websockets, PyYAML. There is no PostgreSQL driver, no Redis client, and no queue
library.

**Capability matching exists; inventory does not.** `placement.place(job, hosts)` is a real
capability gate with a measured-provenance token, and it is good. But `hosts` is supplied by
the caller — nothing builds that list from a durable registry, nothing persists a profile, and
#38's own review notes that `place()` is *not wired into the dispatch path*. It is a rule that
exists rather than a rule that runs.

**A worker abstraction exists and is local-only.** `research/workers.py` defines the
`ResearchWorker` protocol, `WorkerSpec`, `WorkerCapability` and `WorkerTrust`;
`worker_adapter.SubprocessWorker` already checks `requirements=` against the local host before
spawning. That is the extension point for remote dispatch, and it should be extended rather
than replaced.

**Host-dependent pieces are already split by platform.** systemd user units in `deploy/omen/`
and `deploy/pi/`; `scripts/setup-env.ps1`, `scripts/install-daemon-task.ps1` and
`scripts/quantbot-watchdog.cmd` on Windows; `sandbox/wsl.py` for Linux-only sandboxing.

**Node registration does not exist in any form.** Capability *detection* did, partially, and
now exists properly. Identity, persistence, heartbeat and a registry do not.

## Decisions

### No Docker, PostgreSQL, Redis or message broker

The brief listed these as candidates. They are declined for now, and this is the one place the
plan departs from what was asked.

CLAUDE.md forbids introducing distributed infrastructure "unless measurable requirements
justify them", and there is no measurement here that does. The workload is one always-on
control plane, a handful of dispatched research jobs, and a single-writer SQLite ledger whose
correctness properties (the lock, the migration authority, reconciliation freshness) are built
around exactly that. Swapping the store for PostgreSQL would invalidate a body of tested
behaviour to gain concurrency this system does not yet have, and adding Redis would introduce a
second source of truth for a queue that currently has one producer.

Containers are declined on a narrower ground: they would be genuinely useful for research
sandboxing, and `sandbox/` already solves that with WSL and OS-enforced limits. Adding Docker to
run the *trading daemon* buys isolation nothing is asking for and costs a runtime dependency on
every node.

This is reversible. If a second coordinator, a remote worker fleet, or concurrent writers ever
become real requirements, that is a measured justification and the decision changes then. The
installer must therefore *detect* container and WSL availability — `quantbot.node.capabilities`
already does — without *requiring* it.

### Roles are assigned; capability is measured

Implemented in `node/policy.py`. Hardware says what a machine could carry, configuration says
what it will. If the only way to restrict a powerful node were to understate its hardware, the
capability system would be teaching operators to lie to it.

`suggested_role` never proposes `COORDINATOR`. Exactly one node holds the ledger behind a
single-writer lock, and which one is not inferable from specs.

### Detection is separate from policy

Also implemented. `capabilities` reports `AMD Radeon RX 6650M, 8176 MB`; `policy` reports *not
CUDA-eligible*. The previous probe collapsed these — `placement._gpu_vram_mb` returns `0` when
`nvidia-smi` is absent, which erases the card entirely.

### `deploy/omen/` is misnamed and must become role-based

**This matters more than it looks.** `docs/runtime-topology.md` records `omen-windows` as 32
cores / 32,486 MB / 16,376 MB VRAM. That is the **i9-14900F + RTX 4080 Super desktop**, not the
HP Omen *laptop* added in 2026-08. Both machines are HP Omens. The existing deployment directory
and the existing host labels are ambiguous as of now, and a second machine is exactly the event
that exposes it.

Directories and labels must key on *role*, not on a product name that two machines share.

### State lives outside the working tree

`QUANTBOT_STATE_DIR` names the volume holding databases, logs, datasets, models, telemetry and
the identity file. Existing variables keep priority, so a working installation is unaffected.
`reports` keeps its name because `QUANTBOT_REPORTS_DIR` already uses it; there is no `docker`
directory because nothing is containerised.

On a machine with a second disk the installer should point this at it. On the Omen that is
`D:\Elrond`.

## Backlog

Dependency-ordered. Each is independently reviewable. Parent: #38. Roadmap: #2.

| # | Work | Depends on |
|---|---|---|
| N1 | **Node identity, capability detection, role policy** — `quantbot.node`. *Delivered on `claude/node-bootstrap-38`.* | — |
| N2 | **Reconcile `placement.gpu_vram_mb` with node detection** — derive it from `node.policy.cuda_vram_mb`, rename the concept to say CUDA, keep placement behaviour byte-identical. Deliberately deferred from N1 so an additive commit could not alter a dispatch decision. | N1 |
| N3 | **Durable node registry** — schema, Alembic migration, repository. Registration, capability snapshots, heartbeat freshness. A snapshot is a fact about when it was taken, and staleness must block like `ENDPOINT_FRESHNESS` already does. | N1 |
| N4 | **Node configuration and state layout** — `QUANTBOT_STATE_DIR` precedence, per-node config file, migration path for the relative defaults in `.env.example`. | N1 |
| N5 | **`quantbot node` CLI** — `node info`, `node register`, `node role`. Extends the existing argparse surface; no second binary. | N3, N4 |
| N6 | **Windows bootstrap** — one command from a clean machine: prerequisites, uv, Python, venv, state directories, config, secrets via the existing `setup-env.ps1` conventions, first `doctor`. Modular steps, not one opaque script. | N4, N5 |
| N7 | **Service lifecycle and reboot recovery** — `start`/`stop`/`restart`; Windows Scheduled Task (extend `scripts/install-daemon-task.ps1`), systemd user units for WSL/Linux (extend `deploy/`). | N6 |
| N8 | **`doctor` for node health** — services, ledger connectivity, free disk, registration, heartbeat, credentials present-but-never-printed, clock sanity, schema version, stale services, config drift. | N3, N5 |
| N9 | **Capability-aware dispatch** — wire `place()` into the path that actually starts work, keyed on role *and* measured capability. Closes the "rule that exists rather than runs" gap #38 recorded. CUDA work must never route to a node whose `cuda_vram_mb` is `0`. | N2, N3 |
| N10 | **Idempotency, repair, upgrade, uninstall** — second run confirms, repairs, upgrades, or explains the conflict. Remove-node must deregister rather than orphan. | N6, N7 |
| N11 | **Installer logging** — structured install log; a failure names the step and the log path. | N6 |
| N12 | **Role-based deployment paths** — retire `deploy/omen/`, relabel hosts by role. Resolves the collision above. | N7 |
| N13 | **Documentation** — architecture, install, supported platforms, roles, hardware guidance, ports, storage, upgrade, repair, uninstall, troubleshooting, `doctor`, adding and removing a node. | N6–N12 |
| N14 | **Clean-machine acceptance test** — the Omen, from unconfigured to registered, `doctor` green, a routed test workload, a refused CUDA workload, reboot recovery, a second install proving idempotency, and an upgrade. | N10, N13 |

## Networking

Current listeners, both loopback-only by design: dashboard `127.0.0.1:8080`, control surface
`127.0.0.1:8081`.

No port becomes LAN-reachable in N1–N8. If N9 introduces remote dispatch, the node-to-node
channel must be authenticated before it binds to anything other than loopback, and every
reachable port has to be documented in N13 with the reason it is open.

## Safety

`LIVE_TRADING` is untouched by all of this. Installing a node must never enable it, and
`deploy/omen/setup.sh` already refuses to deploy against an env file that sets it — that check
belongs in the new installer too.

Adding a node adds capacity, never authority. A new node cannot clear a kill switch, cannot
ratify a mandate, and cannot become coordinator by describing itself as one.

## Open

- Whether the Omen or the desktop should end up holding the ledger. #38 recorded the desktop's
  WSL2 as the coordinator; the brief for the second machine says the always-on laptop should
  carry infrastructure. Both are defensible, the choice is reversible, and N3 has to exist before
  either can be executed safely. Deciding it does not block N2–N8.
- Whether `DEVELOPMENT` and `ALL_IN_ONE` earn their place as distinct roles once N9 makes
  dispatch real, or collapse into `WORKER` with a dispatch flag.
