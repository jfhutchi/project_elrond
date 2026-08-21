"""OS-enforced isolation for generated code, on the machine this project actually runs on (#21).

`runner.py` is honest about being concealment rather than containment: it builds a `sys.path` that
omits the repository, constructs an empty environment, and monkeypatches `socket`. Every one of
those is bypassable by code that knows it is being watched -- an absolute path still resolves, and
`importlib.reload(socket)` undoes the network guard. Its limits are polled by a parent process
rather than imposed by the kernel.

This backend asks the kernel instead, using four primitives verified against this WSL2 instance
before a line of it was written:

1. **Mount namespace.** `mount -t tmpfs none /mnt` inside the namespace replaces the Windows drive
   mounts with an empty directory. The repository, `.env`, the durable ledger and the production
   locks all live under `/mnt/e/...`, so after that mount they do not exist for the child -- not
   hidden, not permission-denied, *absent* -- whatever path it is handed. Verified with a control:
   the same read succeeds outside the namespace and fails inside it, which is the difference
   between a guard and a coincidence.

2. **Network namespace.** With `--net` the child gets a fresh stack holding a single DOWN loopback
   and no route anywhere. There is nothing to monkeypatch around, because there is nothing to
   monkeypatch: the syscall fails in the kernel. This is #12's outstanding network box.

3. **`RLIMIT_AS`.** An allocation past the ceiling returns failure to the allocator, so the child
   raises `MemoryError` at the moment it asks rather than being killed some poll interval later.
   Enforcement does not depend on anyone noticing.

4. **`RLIMIT_CPU`.** The kernel sends SIGKILL. Verified: returncode -9.

**What is deliberately not claimed.** `systemd-run --user --scope -p MemoryMax=` was tried first
and does *not* enforce here -- the user session has no delegated memory controller, and the
allocation succeeded -- so cgroup limits are not used and not mentioned in any docstring as though
they were. `RLIMIT_AS` bounds address space, which is not the same as resident memory; a process
that maps a large file without touching it is bounded by this and would not be bounded by an RSS
ceiling, and vice versa.

**Detection versus enforcement, kept apart.** A script that catches its own `MemoryError` will not
be reported as a memory termination, because nothing above it can tell. It still did not get the
memory. The ceiling holds either way, and the tests assert the ceiling rather than the label --
asserting the label alone would pass a sandbox that reported nicely and enforced nothing.
"""

from __future__ import annotations

import base64
import hashlib
import io
import shlex
import subprocess
import tarfile
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from quantbot.sandbox.policy import SandboxPolicy
from quantbot.sandbox.runner import Artifact, SandboxError, SandboxResult
from quantbot.sandbox.static import enforce

DEFAULT_DISTRO = "Ubuntu"

#: Where a workspace lives. Inside the distribution's own filesystem, never under `/mnt`: a
#: workspace on the Windows drives would be masked by the very mount that hides the repository,
#: and the child would find its own inputs missing.
WORKSPACE_ROOT = "/tmp/elrond-sandbox"

#: How a CPU-ceiling death arrives. `RLIMIT_CPU` raises SIGXCPU at the soft limit and SIGKILL at
#: the hard one, and `ulimit -t` sets both to the same value, so either can be the one that lands.
#: bash reports a signalled child as 128+N; a raw negative code appears when nothing translates it.
_CPU_KILL_CODES = frozenset({-9, 137, -24, 152})


class WslUnavailable(SandboxError):
    """Raised when the WSL backend was asked for and cannot be provided."""


#: Injected for tests, as every other transport in this project is. A sandbox whose only
#: test path is the real one is a sandbox nobody tests the failure modes of.
Runner = Callable[..., Any]


def _run(
    command: Sequence[str],
    *,
    timeout: float,
    stdin: bytes | None = None,
    runner: Runner | None = None,
) -> Any:
    execute = runner or subprocess.run
    return execute(
        list(command),
        input=stdin,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def wsl_backend_available(*, distro: str = DEFAULT_DISTRO, runner: Runner | None = None) -> bool:
    """Whether a usable distribution and unprivileged namespaces are actually present.

    Checked by running the thing rather than by looking for `wsl.exe`. The executable ships with
    Windows and is present on machines with no distribution installed at all, where every command
    returns empty and a caller that trusted the binary's existence would build a sandbox backend
    on top of nothing.
    """
    try:
        probe = _run(
            [
                "wsl.exe",
                "-d",
                distro,
                "-e",
                "unshare",
                "--user",
                "--map-root-user",
                "--mount",
                "--net",
                "--pid",
                "--fork",
                "--",
                "/bin/bash",
                "-c",
                # Probes every ceiling this backend depends on, not just the namespace. A distro
                # whose shell silently drops one of these must not report itself as available.
                "set -e; mount -t tmpfs none /mnt; ulimit -v 262144; ulimit -t 30;"
                " ulimit -u 64; echo READY",
            ],
            timeout=60,
            runner=runner,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0 and b"READY" in probe.stdout.replace(b"\x00", b"")


class WslSandbox:
    """Run generated code inside WSL under kernel-enforced isolation.

    Interface-compatible with `SandboxRunner.run` on purpose: the two are alternative backends for
    one contract, and a caller should be able to swap them without learning a second vocabulary.
    """

    def __init__(
        self,
        policy: SandboxPolicy | None = None,
        *,
        distro: str = DEFAULT_DISTRO,
        runner: Runner | None = None,
    ) -> None:
        self._policy = policy or SandboxPolicy()
        if self._policy.network_enabled:
            # Refused rather than half-enforced. Filtering a namespace down to an allowlist needs
            # a veth pair and a NAT rule, which need root; without that, "network enabled" here
            # would mean the whole namespace opened and only the in-process allowlist standing --
            # the weaker Windows guarantee, wearing this backend's stronger name. A caller who
            # genuinely needs egress can use `SandboxRunner`, whose ceiling is stated in its own
            # docstring. The intended path is the one this design already takes: inputs are
            # copied in before the child starts, so a normal experiment needs no network at all.
            raise WslUnavailable(
                "the WSL backend does not offer network access: an allowlist inside a network "
                "namespace needs privileges this process does not have, and opening the "
                "namespace would give a weaker guarantee under a stronger name"
            )
        self._distro = distro
        self._runner = runner

    def run(self, source: str) -> SandboxResult:
        """Execute `source`. Raises only when the sandbox itself fails, never the script."""
        # Static scan first, exactly as the Windows backend does. It is defence in depth and
        # trivially bypassable, which is why it is first and not last.
        enforce(source)

        workspace = f"{WORKSPACE_ROOT}-{uuid.uuid4().hex}"
        digest = _digest_bytes(source.encode("utf-8"))
        started = time.monotonic()
        try:
            self._prepare(workspace, source)
            completed, reason = self._execute(workspace)
            duration = time.monotonic() - started
            artifacts = self._collect(workspace)
        finally:
            self._cleanup(workspace)

        stdout = _text(completed.stdout)
        stderr = _text(completed.stderr)
        if reason is None:
            reason = _termination(completed.returncode, stderr)
        return SandboxResult(
            ok=reason is None and completed.returncode == 0,
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            peak_memory_mb=None,
            artifacts=artifacts,
            terminated_reason=reason,
            script_sha256=digest,
        )

    # -- steps ---------------------------------------------------------------------------------

    def _prepare(self, workspace: str, source: str) -> None:
        """Create the workspace and write the script, passing the source as base64.

        Base64 rather than a heredoc or an argument: the source is arbitrary text that will cross
        a Windows argv, a WSL argv and a shell, and every quoting scheme between here and there
        has a character that breaks it. A base64 literal has none.
        """
        encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
        script = (
            f"set -eu\n"
            f"mkdir -p {shlex.quote(workspace)}/outputs\n"
            f"printf %s {shlex.quote(encoded)} | base64 -d"
            f' > "{workspace}/experiment.py"\n'
        )
        completed = self._wsl(["bash", "-s"], stdin=script.encode("utf-8"), timeout=60)
        if completed.returncode != 0:
            raise WslUnavailable(f"could not prepare a workspace in {self._distro}")

    def _execute(self, workspace: str) -> tuple[Any, str | None]:
        """Run the experiment under namespaces and rlimits. Returns the result and any reason."""
        inner = "\n".join(
            [
                # `set -e` is load-bearing rather than habit. The first version ran under
                # `/bin/sh` -- dash on Ubuntu -- where `ulimit -u` is not a supported option.
                # dash printed "Illegal option -u" and carried straight on to exec the
                # experiment with no process limit at all. A ceiling that fails to apply and
                # does not say so is the exact failure this backend exists to remove, so any
                # ulimit that will not take now aborts the run instead of being skipped.
                "set -e",
                # The mount that makes the Windows drives -- and with them the repository, the
                # dotenv, the ledger and the locks -- cease to exist for this process.
                "mount -t tmpfs none /mnt",
                f"cd {shlex.quote(workspace)}",
                f"ulimit -v {self._policy.memory_mb * 1024}",
                # Equal to the wall clock, with the subprocess timeout sitting above both. A
                # spinning process therefore exhausts CPU first and is killed by the kernel,
                # while the wall clock stays the outer bound for a process that is blocked
                # rather than burning.
                f"ulimit -t {max(1, int(self._policy.wall_clock_seconds))}",
                "ulimit -u 64",
                # -I isolates the interpreter, -S skips site-packages, -B writes no bytecode.
                #
                # Deliberately not `exec`. With `--pid --fork`, unshare is PID 1 of the new
                # namespace, and when the kernel SIGKILLs the child for exceeding RLIMIT_CPU
                # unshare reports "sigprocmask unblock failed" and exits 1 -- the ceiling fires
                # and the caller cannot tell it apart from an ordinary error. Letting bash stay
                # in the middle costs one process and makes the death visible: bash reports a
                # signalled child as 128+N, so a CPU kill arrives as 137 (or 152 for SIGXCPU)
                # instead of being flattened into a generic failure.
                "python3 -I -S -B experiment.py",
                "exit $?",
            ]
        )
        # `--net` is unconditional: this backend refuses a network-enabled policy at
        # construction, so there is no configuration in which the stack is left open.
        namespaces = ["--user", "--map-root-user", "--mount", "--net", "--pid", "--fork"]
        # bash, not sh. See the `set -e` note above: dash silently lacks `ulimit -u`.
        command = ["unshare", *namespaces, "--", "/bin/bash", "-c", inner]
        try:
            return self._wsl(command, timeout=self._policy.wall_clock_seconds + 15), None
        except subprocess.TimeoutExpired as expired:
            self._reap(workspace)
            return _TimedOut(expired), "wall-clock"

    def _collect(self, workspace: str) -> tuple[Artifact, ...]:
        """Bring the outputs back as a tar on stdout, so one round trip carries every file."""
        completed = self._wsl(
            ["tar", "-C", workspace, "-cf", "-", "outputs"],
            timeout=120,
        )
        if completed.returncode != 0:
            return ()
        artifacts: list[Artifact] = []
        budget = self._policy.max_output_bytes
        with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                payload = handle.read(budget + 1)
                if len(payload) > budget:
                    raise SandboxError(
                        f"sandbox outputs exceeded {self._policy.max_output_bytes} bytes"
                    )
                budget -= len(payload)
                artifacts.append(
                    Artifact(
                        name=Path(member.name).name,
                        size_bytes=len(payload),
                        sha256=_digest_bytes(payload),
                    )
                )
        return tuple(artifacts)

    def _cleanup(self, workspace: str) -> None:
        """Destroy the workspace. A leftover could be read by the next experiment."""
        try:
            self._wsl(["rm", "-rf", workspace], timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass

    def _reap(self, workspace: str) -> None:
        """Kill anything still running for this workspace after a wall-clock stop.

        Terminating `wsl.exe` on the Windows side does not reliably reach the Linux process, and
        an experiment that outlives its own timeout is exactly what the timeout exists to stop.
        """
        try:
            self._wsl(["pkill", "-9", "-f", workspace], timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass

    def _wsl(
        self, command: Sequence[str], *, timeout: float, stdin: bytes | None = None
    ) -> Any:
        return _run(
            ["wsl.exe", "-d", self._distro, "-e", *command],
            timeout=timeout,
            stdin=stdin,
            runner=self._runner,
        )


class _TimedOut:
    """Stands in for a completed process that never completed."""

    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, expired: subprocess.TimeoutExpired) -> None:
        self.returncode = None
        self.stdout = expired.stdout or b""
        self.stderr = expired.stderr or b""


def _text(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    # WSL interleaves NULs into some console output; they are not part of what the script wrote.
    return raw.replace(b"\x00", b"").decode("utf-8", "replace")


def _digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _termination(returncode: int | None, stderr: str) -> str | None:
    """Name why a run stopped, from evidence rather than assumption.

    `MemoryError` in stderr is a *report* that the ceiling held, not the enforcement itself. A
    script that swallows its own `MemoryError` gets no label here and still gets no memory, which
    is why the tests assert the ceiling rather than this string.
    """
    if returncode is None:
        return "wall-clock"
    if returncode in _CPU_KILL_CODES:
        return "cpu"
    if returncode != 0 and stderr.strip().endswith("MemoryError"):
        return "memory"
    return None


__all__ = [
    "DEFAULT_DISTRO",
    "WORKSPACE_ROOT",
    "WslSandbox",
    "WslUnavailable",
    "wsl_backend_available",
]
