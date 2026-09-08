# Multi-node deployment

How a second machine becomes an Elrond node, and what has to exist before that is routine
rather than a bespoke project each time. Extends [`runtime-topology.md`](runtime-topology.md)
and #38.

> **Current policy (2026-09-08):** Elrond requires one native Windows Omen only. The Pi is retired
> entirely and must not be contacted; the Alienware and any future worker are optional capacity,
> never dependencies. This document preserves the earlier multi-node design as history.

## Status

`quantbot.node` exists: identity, capability detection, role policy. The Omen's local-account
startup tasks, PAPER ledger, reconciliation and loopback model service have been exercised.
New trading is halted by the kill switch after a broker rejection; the tested entry-budget fix
awaits Windows elevation for deployment. See [current evidence](omen-runtime-2026-09-08.md).
The backlog below is historical planning unless a future measured need justifies another node.

## What the repository actually assumes today

Recorded because three of these contradict what a multi-node installer would naturally assume,
and building against the assumption rather than the code would have produced a plausible
installer for a system that does not exist.

**One machine owns everything.** `Settings` reads process environment variables only
(`env_file=None`). The ledger is SQLite behind a single-writer lock (`storage/lock.py`). The
default paths in `.env.example` are *relative* — `quantbot.db`, `reports`, `quantbot.lock` —
so durable state follows whichever directory the process started in.

**Nothing is containerised.** There is no Dockerfile and no compose file, and nothing in the tree
runs a container: `node/capabilities.py` *detects* one, `sandbox/policy.py` names it as a
backend, and `research/dispatch.py` lists Docker as a future target behind the same protocol.
The runtime dependency set is SQLAlchemy, Alembic, httpx, pydantic, pydantic-settings, PyYAML,
structlog, websockets, tzdata, and — for the operator console — FastAPI, Jinja2 and uvicorn.
There is no PostgreSQL driver, no Redis client, and no queue library. (This paragraph previously
said "no container reference anywhere in the tree" and listed seven dependencies, omitting the
three that serve a network listener. Both are corrected; an installer written from the old list
would not have known this repository serves HTTP.)

**Capability matching exists; inventory does not.** `placement.place(job, hosts)` is a real
capability gate with a measured-provenance token, and it is good. But `hosts` is supplied by
the caller and nothing builds that list from a durable registry or persists a profile.

*Partly superseded by #136:* `research/dispatch.py` now puts a `WorkerDispatcher` in front of
the backends and asks each one `can_run(requirements)`, built on placement's `unmet_requirement`
and `NoCapableHost`. So the capability rule *does* run on the path that starts work — but with
exactly one backend (`SubprocessWorker`, this machine), so it can still only ever refuse, never
route. The sentence this replaces said `place()` was "a rule that exists rather than a rule that
runs", which stopped being true when the dispatcher landed.

**A worker abstraction exists and is local-only.** `research/workers.py` defines the
`ResearchWorker` protocol, `WorkerSpec`, `WorkerCapability` and `WorkerTrust`;
`worker_adapter.SubprocessWorker` already checks `requirements=` against the local host before
spawning. That is the extension point for remote dispatch, and it should be extended rather
than replaced.

**Host-dependent pieces are split by platform.** The current path is native Windows Task Scheduler
through `deploy/omen/setup-windows.ps1`. The systemd units under `deploy/omen/` and `deploy/pi/`
are historical; the Pi installer now refuses immediately. `sandbox/wsl.py` remains a Linux-only
sandbox backend, but WSL is absent on the measured Omen.

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

## Historical networking evidence

> **Corrected 2026-09-02.** This section said "Current listeners, both loopback-only by design:
> dashboard `127.0.0.1:8080`, control surface `127.0.0.1:8081`." Neither half was true. Both bind
> `0.0.0.0` on purpose — a LAN dashboard is the entire point of the Pi deployment — and there are
> three listeners, not two. Getting a listener's bind address wrong in the document an installer
> would be written from is the failure mode this file exists to prevent.

| Listener | Binds | Confined by |
|---|---|---|
| Pi dashboard, port 8080 | `0.0.0.0` (`dashboard.service`) | systemd `IPAddressAllow` = localhost + RFC1918 only, `IPAddressDeny=any`. Static files, no credentials, no `EnvironmentFile` |
| Pi control surface, port 8081 | `0.0.0.0` (`scripts/control_server.py`) | Same systemd address filter, plus a bearer token. Holds the ledger, not the broker keys |
| Operator console, port 8787 | `127.0.0.1` (`quantbot console`) | Loopback default; `--host` widens it deliberately |

Those Pi listeners were part of the retired topology and are not current dependencies. They remain
documented because they describe the prior attack surface. The current Omen model service binds
only to `127.0.0.1:11434`; remote dispatch would require authentication before binding beyond
loopback, and every reachable port must be documented with its purpose.

## Safety

Live trading is untouched by all of this. Installing a node must never enable it.

The active native Windows path validates PAPER-only authority through the application parser in
`quantbot.operations.service`, invoked by `deploy/omen/setup-windows.ps1` and the service wrapper.
The shell guard `deploy/refuse-live-trading.sh` remains for historical shell deployment paths;
it is not the native Windows startup boundary. The retired Pi installer exits immediately before
reading credentials or inspecting the host. The shell guard can also be checked directly:

```bash
bash deploy/refuse-live-trading.sh ~/.config/quantbot/quantbot.env
```

It refuses a present-and-not-`PAPER` `TRADING_MODE` or `BROKER_ENVIRONMENT`, and any spelling of
`LIVE_TRADING_ACKNOWLEDGED` that pydantic reads as true. Absence is accepted, because both
settings already default to `PAPER`. A lowercase `paper` is refused: `TradingMode` is a StrEnum
whose only value is `PAPER`, so anything else fails to parse, and a value that would crash the
daemon at startup should not get as far as being installed. Exit 1 is *"looked, and no"*; exit 2
is *"could not look"*, kept distinct so an unreadable file cannot read as a clean one.

> **Fixed 2026-09-03. What it replaced is worth keeping, because it is this file's own failure
> mode.** This section used to say "`deploy/omen/setup.sh` already refuses to deploy against an
> env file that sets it". The script did contain a check — and the check grepped for
> `LIVE_TRADING=true`. **There is no `LIVE_TRADING` setting.** `Settings` reads `TRADING_MODE`
> and `LIVE_TRADING_ACKNOWLEDGED` with `extra="ignore"`, so a variable named `LIVE_TRADING` is
> discarded by the config layer and read by nothing else in the tree. The regex could not match
> `LIVE_TRADING_ACKNOWLEDGED` either, because it required `=` immediately after `LIVE_TRADING`.
> An env file setting `TRADING_MODE=LIVE` passed silently while the script reported that it had
> refused.
>
> `deploy/pi/setup.sh` was worse and quieter: it had **no** check. It writes a paper env file,
> but only `if [ ! -f "$ENV_FILE" ]`, so an existing live one survived untouched.
>
> The real boundary was intact throughout — `TradingMode.LIVE` without acknowledgement is refused
> in `config.py:140`, and no agent may supply live credentials — so this was a guard that proved
> nothing rather than an open door. It is exactly the class CLAUDE.md's engineering standard is
> written about: *a check that passes for a reason unrelated to the property it claims*.
>
> One file with two callers, rather than two copies, because duplicated safety checks drift and
> the drift is invisible until the day it matters. `tests/unit/test_deployment_live_trading_guard.py`
> asserts the **named** refusal with a control for each case, and was confirmed to fail — 18 of
> 31 cases — when the old logic was put back.

Historically, `deploy/pi/setup.sh` also wrote `TRADING_MODE=PAPER` and
`LIVE_TRADING_ACKNOWLEDGED=false` into the env file it generated. That implementation evidence is
retained above; the current script creates nothing.

Adding a node adds capacity, never authority. A new node cannot clear a kill switch, cannot
ratify a mandate, and cannot become coordinator by describing itself as one.

## Open

- The host choice is resolved by operator policy: the native Windows Omen laptop is the sole
  permitted coordinator. The older desktop/WSL proposal remains historical evidence and grants no
  current authority.
- Whether `DEVELOPMENT` and `ALL_IN_ONE` earn their place as distinct roles once N9 makes
  dispatch real, or collapse into `WORKER` with a dispatch flag.
