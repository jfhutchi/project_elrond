"""OS-enforced isolation for generated code on Windows, without WSL or Docker (#21).

`wsl.py` provides the kernel-enforced backend the research callers require, and on the machine
Elrond actually runs on -- an OMEN laptop on Windows 11 Home -- it cannot run at all. There is no
WSL distribution installed, Windows Sandbox is unavailable on a Home SKU, and Docker is absent.
That left `SandboxRunner`, which is honest about being concealment rather than containment, and
which `mine_for_anomalies` and `codegen` therefore refuse. Autonomous research could not execute.

This backend closes that without adding a prerequisite to the host. Both primitives ship with
Windows and neither needs elevation:

1. **AppContainer.** The child runs under a token with an AppContainer SID and *no capabilities*.
   An AppContainer token fails the access check against any object whose ACL does not name its
   SID, so the filesystem is default-deny: the per-run workspace and a read-only interpreter cache
   are granted, and nothing else is. The repository, `.env`, the durable store and the user
   profile are unreachable whatever path the generated code constructs. That is the precise
   property `mining.py` refuses `SandboxRunner` for, and it is enforced by the kernel rather than
   by a `sys.path` the child can rebuild.

   Network denial falls out of the same emptiness. Without `internetClient` the Windows Filtering
   Platform refuses the socket: `getaddrinfo` fails and `connect` returns `WinError 10013`. There
   is nothing in-process to undo, which is the difference between this and `PREAMBLE`'s
   monkeypatched `socket`.

2. **Job Object.** `JOB_OBJECT_LIMIT_PROCESS_MEMORY` makes the memory ceiling allocation-time --
   the child raises `MemoryError` when it asks, as `RLIMIT_AS` does under WSL, rather than being
   killed some poll interval later. `ActiveProcessLimit` caps process count, which `policy.py`
   recorded as impossible "on this platform without a Job Object". `KILL_ON_JOB_CLOSE` makes tree
   termination atomic: closing the handle kills every descendant, so a script that spawns helpers
   cannot outlive its own run.

**Fail closed.** Every way of failing to establish isolation raises `WindowsUnavailable` before
the child runs. There is no fallback to `SandboxRunner` and no fallback to unrestricted host
execution: a caller that asked for an OS-enforced backend and cannot have one gets an exception,
never a weaker sandbox wearing the stronger name. `network_enabled=True` is refused at
construction for the same reason `WslSandbox` refuses it -- `internetClient` grants all internet
rather than an allowlist, and honouring the flag would open everything while the policy text still
described a restriction.

**What is not claimed.** A mount namespace makes the repository *absent*; this makes it *access
denied*, so generated code can tell it is sandboxed. The Job Object ceiling bounds committed
memory where `RLIMIT_AS` bounds address space; neither is strictly stronger.
`docs/sandbox-contract.md` records both, and the differences do not reach the contents the
contract protects.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

from quantbot.sandbox import _win32
from quantbot.sandbox.policy import SandboxPaths, SandboxPolicy
from quantbot.sandbox.runner import (
    Artifact,
    SandboxError,
    SandboxResult,
    _approved_package_paths,
    _child_environment,
    _collect,
)
from quantbot.sandbox.static import enforce


class WindowsUnavailable(SandboxError):
    """Raised when the Windows backend was asked for and cannot be provided."""


#: Where workspaces and the interpreter cache live. Under `LOCALAPPDATA` rather than `TEMP` so a
#: leftover is attributable to this project and a cleanup sweep has an unambiguous root, and
#: outside the repository so nothing the child is granted is inside the checkout.
SANDBOX_ROOT = Path(
    os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
) / "elrond-sandbox"

#: Marks a staged interpreter as complete. Written last, so a copy interrupted halfway is not
#: mistaken for a usable runtime on the next run.
_STAGED_MARKER = ".elrond-staged"

#: How much CPU time the job allows relative to the wall clock. See the comment at the call site:
#: the two ceilings must not be able to fire at the same moment, or the reported reason is noise.
_CPU_HEADROOM = 2

#: Processes the job permits. One interpreter, plus room for something the standard library
#: starts on its own behalf. Low enough that a fork bomb is refused by the kernel at the fourth
#: process rather than by anyone noticing.
_ACTIVE_PROCESS_LIMIT = 4

#: Runs before the generated source. Shorter than `runner.PREAMBLE` because the two things that
#: file monkeypatches -- `sys.path` and `socket` -- are handled by the kernel here. What remains
#: is the integrity check, kept for the same reason it exists there: if `quantbot` turns out to be
#: importable the run aborts rather than continuing in a state the caller believes is isolated.
PREAMBLE = '''import sys as _sys, os as _os, importlib.util as _u
_packages = _os.path.join(_os.getcwd(), "_packages")
if _os.path.isdir(_packages):
    _sys.path.insert(0, _packages)
if _u.find_spec("quantbot") is not None:
    raise SystemExit("SANDBOX INTEGRITY FAILURE: quantbot is importable inside the sandbox")
'''


def _base_interpreter() -> Path:
    """The interpreter to stage: the base one, never a repository-local virtualenv."""
    executable = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    if not executable.is_file():
        raise WindowsUnavailable(f"base Python interpreter does not exist: {executable}")
    return executable


def _fingerprint(executable: Path) -> str:
    """Identify a staged runtime by the interpreter it was copied from.

    Path, size and mtime rather than a hash of 80MB of files: the cost of hashing the tree on
    every run would exceed the cost of the experiments, and an interpreter that changed in place
    without changing size or mtime is not a case this needs to survive.
    """
    stat = executable.stat()
    material = f"{executable}|{sys.version}|{stat.st_size}|{stat.st_mtime_ns}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def stage_runtime(container: _win32.AppContainer) -> Path:
    """Copy the interpreter somewhere the AppContainer can execute it, once per interpreter.

    The alternative was granting the AppContainer read access to the interpreter where it already
    lives. That is one `icacls` call instead of an 80MB copy, and it was rejected: the base
    interpreter here sits inside the user profile under a package manager's control, so the grant
    would modify a directory this project does not own and would vanish the next time that tool
    replaced it. A cache this project creates is one this project can reason about.

    Staging is atomic by rename, so two runs starting together cannot read a half-copied tree.
    """
    executable = _base_interpreter()
    cache = SANDBOX_ROOT / "runtime" / _fingerprint(executable)
    stamp = hashlib.sha256(container.sid_string.encode("utf-8")).hexdigest()[:12]
    granted = cache / f".elrond-granted-{stamp}"
    if (cache / _STAGED_MARKER).is_file() and granted.is_file():
        return cache

    if not (cache / _STAGED_MARKER).is_file():
        staging = SANDBOX_ROOT / "runtime" / f".staging-{uuid.uuid4().hex}"
        staging.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(executable.parent, staging, symlinks=False)
            (staging / _STAGED_MARKER).write_text("ok", encoding="utf-8")
            try:
                staging.rename(cache)
            except OSError:
                # Another run staged the same fingerprint first. Its copy is as good as this one.
                shutil.rmtree(staging, ignore_errors=True)
        except OSError as error:
            shutil.rmtree(staging, ignore_errors=True)
            raise WindowsUnavailable(f"could not stage a sandbox interpreter: {error}") from error

    if not (cache / _STAGED_MARKER).is_file():
        raise WindowsUnavailable(f"sandbox interpreter cache is incomplete at {cache}")

    if not granted.is_file():
        try:
            _win32.grant_path(cache, container.sid_string, "RX")
            granted.write_text("ok", encoding="utf-8")
        except OSError as error:
            raise WindowsUnavailable(
                f"could not grant the sandbox interpreter to {container.sid_string}: {error}"
            ) from error
    return cache


def windows_backend_available() -> bool:
    """Whether this host can actually provide the isolation, not whether it looks like Windows.

    Exercises both primitives rather than checking for a DLL: a `CreateAppContainerProfile` that
    returns an error and a `SetInformationJobObject` that refuses the memory flag are the two ways
    this fails on a machine where `sys.platform` still says `win32`. Deliberately stops short of
    staging the interpreter, which costs an 80MB copy the first time and belongs to the run.
    """
    if sys.platform != "win32":
        return False
    try:
        _win32.app_container()
        with _win32.JobObject(memory_bytes=64 * 1024 * 1024, active_process_limit=4):
            return True
    except OSError:
        return False


def _termination(exit_status: int, stderr: str) -> str | None:
    """Name why a run stopped, from evidence rather than assumption.

    `MemoryError` in stderr is a *report* that the ceiling held, not the enforcement itself, and a
    script that swallows its own gets no label here and still gets no memory. The tests therefore
    assert the ceiling rather than this string, exactly as the WSL backend's do.

    `runner.py` deliberately never returns `memory` on Windows, because there the ceiling was
    polled and a `MemoryError` came from the script's own bad luck rather than from a limit. Here
    the Job Object refuses the allocation, so the same evidence means the opposite thing.
    """
    if exit_status == _win32.ERROR_NOT_ENOUGH_QUOTA:
        return "cpu"
    if exit_status != 0 and stderr.strip().endswith("MemoryError"):
        return "memory"
    return None


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return ""


class WindowsSandbox:
    """Run generated code on Windows under kernel-enforced isolation.

    Interface-compatible with `SandboxRunner.run` and `WslSandbox.run` on purpose: the three are
    alternative backends for one contract, and a caller should be able to swap them without
    learning a second vocabulary.
    """

    #: This backend's isolation comes from the kernel, so it may be handed a miner. Stated as a
    #: class attribute rather than a constructor argument for the same reason `WslSandbox` does:
    #: a backend cannot become OS-enforced by configuration.
    os_enforced = True

    def __init__(
        self,
        policy: SandboxPolicy | None = None,
        *,
        container_name: str = _win32.DEFAULT_CONTAINER_NAME,
    ) -> None:
        self._policy = policy or SandboxPolicy()
        # Policy first, platform second. A policy this backend can never honour is wrong
        # everywhere, and checking it before the platform means the refusal is the same sentence
        # on the CI runner as on the host -- so the fail-closed property is testable off Windows
        # rather than only where it happens to matter.
        if self._policy.network_enabled:
            # Refused rather than half-enforced, on the same reasoning as `WslSandbox`. The only
            # switch available is the `internetClient` capability, which grants the whole internet
            # and not an allowlist. Turning it on would mean `resolved_hosts()` describing a
            # restriction that does not exist -- the weaker Windows guarantee wearing this
            # backend's stronger name.
            raise WindowsUnavailable(
                "the Windows backend does not offer network access: the only capability available "
                "grants all egress rather than an allowlist, and enabling it would give a weaker "
                "guarantee under a stronger name"
            )
        if sys.platform != "win32":
            raise WindowsUnavailable("the Windows backend requires Windows")
        try:
            self._container = _win32.app_container(container_name)
        except OSError as error:
            raise WindowsUnavailable(f"could not establish an AppContainer: {error}") from error

    @property
    def policy(self) -> SandboxPolicy:
        return self._policy

    @property
    def container_sid(self) -> str:
        """The SID an ACL must name to let this sandbox in. Recorded on run evidence."""
        return self._container.sid_string

    def _prepare(self, root: Path, source: str) -> SandboxPaths:
        paths = SandboxPaths(
            root=root,
            inputs=root / "inputs",
            outputs=root / "outputs",
            script=root / "experiment.py",
        )
        paths.inputs.mkdir(parents=True)
        paths.outputs.mkdir(parents=True)
        (root / "tmp").mkdir()
        paths.script.write_text(PREAMBLE + "\n" + source, encoding="utf-8")
        # Staged the same way `SandboxRunner` stages them, so an experiment that imports an
        # approved package behaves identically on either backend. Absent packages stay absent:
        # the child gets the same clear ImportError rather than a different one per backend.
        _approved_package_paths(self._policy, root / "_packages")
        return paths

    def run(self, source: str) -> SandboxResult:
        """Execute `source`. Raises only when the sandbox itself fails, never the script."""
        # Static scan first, exactly as the other backends do. Defence in depth and trivially
        # bypassable, which is why it is first and not last.
        enforce(source, allowed_third_party=self._policy.allowed_third_party)
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()

        runtime = stage_runtime(self._container)
        interpreter = runtime / "python.exe"
        if not interpreter.is_file():
            raise WindowsUnavailable(f"staged runtime has no interpreter at {interpreter}")

        root = SANDBOX_ROOT / "runs" / uuid.uuid4().hex
        root.mkdir(parents=True)
        try:
            paths = self._prepare(root, source)
            try:
                _win32.grant_path(root, self._container.sid_string, "F")
            except OSError as error:
                raise WindowsUnavailable(
                    f"could not grant the workspace to the sandbox: {error}"
                ) from error
            return self._execute(root, paths, interpreter, digest)
        finally:
            # Deterministic cleanup. A leftover workspace could be read by the next experiment,
            # and the ACE on it names an identity every later run shares.
            shutil.rmtree(root, ignore_errors=True)

    def _execute(
        self, root: Path, paths: SandboxPaths, interpreter: Path, digest: str
    ) -> SandboxResult:
        environment = _child_environment()
        # A writable temp inside the workspace. Without it the child inherits no TEMP at all and
        # anything in the standard library that reaches for one fails for a reason unrelated to
        # the experiment.
        environment["TEMP"] = str(root / "tmp")
        environment["TMP"] = str(root / "tmp")
        # `LOCALAPPDATA` must be *present* or `CreateProcessW` refuses the launch outright with
        # ERROR_ENVVAR_NOT_FOUND: the AppContainer loader reads it while resolving the container's
        # redirected storage, before any of our code runs. Found by minimising the parent
        # environment against a real launch until one variable was left, not from documentation.
        #
        # Only the name is required, not a usable path -- a bogus value and an empty string both
        # launch. It points at the workspace rather than at the real profile directory so that the
        # child's notion of local app data is inside the disposable area rather than beside the
        # host's; the container redirects reads under it into its own package folder regardless.
        #
        # This does *not* hide the host username: the workspace lives under the user profile, so
        # the value still contains it, as do TEMP and TMP, and a child must know its own working
        # directory in any case. Host shape, not host contents. Claiming otherwise here was the
        # first thing the environment test disproved, which is why it is written down.
        environment["LOCALAPPDATA"] = str(root / "tmp")

        stdout_path = root / "_stdout.txt"
        stderr_path = root / "_stderr.txt"
        command = f'"{interpreter}" -I -S "{paths.script.name}"'

        started = time.monotonic()
        job = _win32.JobObject(
            memory_bytes=self._policy.memory_mb * 1024 * 1024,
            active_process_limit=_ACTIVE_PROCESS_LIMIT,
            # A CPU ceiling deliberately looser than the wall clock. The wall clock is the
            # primary deadline; this exists so a script that spins several threads cannot consume
            # many core-seconds per wall second before that deadline arrives.
            #
            # The slack is what keeps the two limits from racing. At parity a single-threaded
            # spin accrues user time at roughly wall rate, so whichever fired first would be a
            # coin toss and an overrun would be labelled `cpu` or `wall-clock` at random. A
            # reason that is right half the time is the kind of instrumentation defect CLAUDE.md
            # says to fix before believing the result.
            cpu_seconds=self._policy.wall_clock_seconds * _CPU_HEADROOM,
        )
        try:
            try:
                launched = _win32.launch(
                    command,
                    cwd=root,
                    environment=environment,
                    container=self._container,
                    job=job,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                )
            except OSError as error:
                raise WindowsUnavailable(
                    f"could not start the sandboxed process: {error}"
                ) from error

            reason: str | None = None
            try:
                if not _win32.wait(launched.handle, self._policy.wall_clock_seconds):
                    reason = "wall-clock"
                    job.terminate()
                    _win32.wait(launched.handle, 10.0)
                status = _win32.exit_code(launched.handle)
            finally:
                _win32.close_handle(launched.handle)

            duration = time.monotonic() - started
            peak_bytes = job.peak_memory_bytes()
        finally:
            # Closing the job kills anything still in it, so nothing the experiment started
            # survives this function even on the paths that raised.
            job.close()

        stdout = _read(stdout_path)
        stderr = _read(stderr_path)
        if reason is None:
            reason = _termination(status, stderr)

        artifacts: tuple[Artifact, ...] = _collect(paths.outputs, self._policy.max_output_bytes)
        return SandboxResult(
            ok=reason is None and status == 0,
            exit_code=status,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            peak_memory_mb=None if peak_bytes is None else round(peak_bytes / (1024 * 1024), 2),
            artifacts=artifacts,
            terminated_reason=reason,
            script_sha256=digest,
        )


__all__ = [
    "PREAMBLE",
    "SANDBOX_ROOT",
    "WindowsSandbox",
    "WindowsUnavailable",
    "stage_runtime",
    "windows_backend_available",
]
