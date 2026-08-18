"""What a sandboxed research experiment is allowed to do, and what it demonstrably cannot.

Design constraint that shapes everything here: this runs on Windows with no Docker, no pywin32,
and no POSIX `resource` module. There is no chroot, no cgroup, no seccomp. Any claim of
OS-enforced isolation would be false, so the design does not rely on one.

Instead the boundary is built from three things that ARE enforceable here:

1. **The project is not importable.** The child runs with the repository off `sys.path`, so
   `import quantbot` fails outright. That removes the broker adapter, the runtime composition
   root, and every order-submission path in one move rather than trying to blocklist them.
2. **Credentials are absent, not merely hidden.** The child environment is built from an empty
   dict upward. Nothing is inherited, so there is no `ALPACA_*` to read. This matters more than
   it sounds: `.env` also lives on disk, so scrubbing environment variables alone would be
   theatre — but a child that cannot import `quantbot`, does not know the repository path, and
   has no dotenv loader has no route to it.
3. **Data arrives as files.** Research inputs are materialised into the workspace as immutable
   snapshots before the child starts, so a normal experiment needs no network at all. Network
   access is opt-in per policy rather than default-available.

Static checks (`static.py`) are defence in depth, not the primary control. A determined
adversary with arbitrary Python and `ctypes` can defeat any in-process check, which is exactly
why the primary controls are absence of capability rather than denial of use.

Honestly stated limits, recorded rather than glossed:

* Memory and CPU ceilings are enforced by an out-of-process monitor that polls and kills. That
  is real but not instantaneous, so a single allocation spike can exceed the ceiling briefly.
* Process count cannot be capped on this platform without a Job Object. A child that spawns
  grandchildren is killed by process-tree termination, but only at the next poll.
* Wall-clock IS hard: it comes from the subprocess timeout.

A WSL backend could make the soft limits hard without changing this contract. The interface is
shaped for that so the upgrade is a backend swap.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

#: Hosts a research experiment may reach when networking is explicitly enabled. Market-data
#: hosts only. The trading host is absent by construction rather than blocklisted, because a
#: blocklist invites the question of what else was forgotten.
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = (
    "data.alpaca.markets",
    "files.stlouisfed.org",
)

#: Never reachable, whatever else a policy says. Order submission lives here, so this is
#: belt-and-braces over the allowlist and is asserted separately by test.
FORBIDDEN_HOSTS: tuple[str, ...] = (
    "paper-api.alpaca.markets",
    "api.alpaca.markets",
    "broker-api.alpaca.markets",
)


class SandboxPolicy(BaseModel):
    """Limits applied to one sandboxed execution. Frozen so a run cannot widen its own bounds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Hard ceiling from the subprocess timeout. The one limit enforced by the OS here.
    wall_clock_seconds: float = Field(default=60.0, gt=0, le=3600)
    #: Polled, not enforced at allocation time. See the module docstring.
    memory_mb: int = Field(default=1024, gt=0, le=16384)
    #: Total bytes the child may write into its output area.
    max_output_bytes: int = Field(default=32 * 1024 * 1024, gt=0)
    #: How often the monitor checks memory. Shorter reacts faster and costs more.
    poll_interval_seconds: float = Field(default=0.25, gt=0, le=5.0)
    #: Networking is off unless a policy turns it on, so the common case needs no allowlist.
    network_enabled: bool = False
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    #: Modules the child may import beyond the standard library. Deliberately short.
    allowed_third_party: tuple[str, ...] = ("numpy", "pandas", "scipy", "statsmodels", "sklearn")

    def forbidden_hosts(self) -> tuple[str, ...]:
        """Hosts denied regardless of the allowlist, so a bad policy cannot open trading."""
        return FORBIDDEN_HOSTS

    def resolved_hosts(self) -> tuple[str, ...]:
        """Effective allowlist: empty when networking is off, and never any forbidden host."""
        if not self.network_enabled:
            return ()
        lowered = {host.lower() for host in FORBIDDEN_HOSTS}
        return tuple(h for h in self.allowed_hosts if h.lower() not in lowered)


class SandboxPaths(BaseModel):
    """Where one execution lives. Everything is inside `root`, which is disposable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root: Path
    #: Immutable snapshots copied in before the child starts. The child may read these.
    inputs: Path
    #: The only location the child may write. Contents are quarantined after the run.
    outputs: Path
    #: The generated script itself, recorded so an artifact can name the code that made it.
    script: Path
