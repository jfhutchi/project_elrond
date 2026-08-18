"""Execute generated research code in a disposable workspace with no route to the broker.

The enforcement story, in the order it matters:

1. **`sys.path` is built, not inherited.** The child runs `python -I -S`, which starts with no
   site-packages and ignores every `PYTHON*` environment variable. The parent then resolves the
   handful of approved scientific packages and passes their directories in explicitly. `quantbot`
   is never among them, so the broker adapter, the runtime composition root, and every
   order-submission path are unreachable — not blocked, absent.
2. **The environment is constructed from empty.** Nothing is inherited. There is no `ALPACA_*`
   to read. This is load-bearing because `.env` also sits on disk: hiding variables alone would
   be theatre, but a child with no dotenv loader, no `quantbot`, and no repository path has no
   route to the file.
3. **Data arrives as files.** Inputs are copied in before the child starts, so a normal
   experiment needs no network. Network is opt-in per policy.
4. **The workspace is disposable and the outputs are quarantined.** Every produced file is
   hashed and recorded so an artifact can name the experiment that made it.

Limits are honest about their strength. Wall-clock is hard, from the subprocess timeout. Memory
is polled by the parent and enforced by killing the process tree, which is real but not
instantaneous. Process count cannot be capped on Windows without a Job Object; grandchildren are
caught by tree termination at the next poll. `policy.py` records this in full.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from quantbot.sandbox.policy import SandboxPaths, SandboxPolicy
from quantbot.sandbox.static import enforce

#: Injected ahead of the generated script. Rebuilds a minimal import path, then proves the
#: boundary from the inside: if `quantbot` turns out to be importable the run aborts rather than
#: continuing in a state the caller believes is isolated.
PREAMBLE = '''\
import sys as _sys, json as _json, pathlib as _pathlib
_cfg = _json.loads(_pathlib.Path("_sandbox.json").read_text(encoding="utf-8"))
_sys.path = [p for p in _cfg["path"] if p]
import importlib.util as _u
if _u.find_spec("quantbot") is not None:
    raise SystemExit("SANDBOX INTEGRITY FAILURE: quantbot is importable inside the sandbox")
if not _cfg["network"]:
    import socket as _socket
    def _denied(*_a, **_k):
        raise PermissionError("sandbox policy: network access is disabled")
    _socket.socket = _denied          # type: ignore[assignment]
    _socket.create_connection = _denied  # type: ignore[assignment]
del _cfg, _u, _sys, _json, _pathlib
'''


class SandboxError(RuntimeError):
    """Raised when the sandbox itself fails, as distinct from the experiment failing."""


@dataclass(frozen=True, slots=True)
class Artifact:
    """One file the experiment produced, content-addressed so it can be cited later."""

    name: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """Outcome of one execution. A non-zero `exit_code` is an experiment failure, recorded."""

    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    artifacts: tuple[Artifact, ...] = field(default_factory=tuple)
    #: Set when the sandbox stopped the run rather than the script finishing on its own.
    terminated_reason: str | None = None
    script_sha256: str = ""

    @property
    def timed_out(self) -> bool:
        return self.terminated_reason == "wall-clock"

    @property
    def exceeded_memory(self) -> bool:
        return self.terminated_reason == "memory"


def _approved_package_paths(policy: SandboxPolicy) -> list[str]:
    """Resolve approved third-party packages to directories, skipping any not installed.

    Resolved in the parent so the child never needs a package index or a path to search. A
    missing package is silently omitted: the experiment will fail at import with a clear message,
    which is better than the sandbox refusing to start.
    """
    paths: list[str] = []
    for name in policy.allowed_third_party:
        spec = importlib.util.find_spec(name)
        if spec is None:
            continue
        locations = list(spec.submodule_search_locations or [])
        for location in locations:
            parent = str(Path(location).parent)
            if parent not in paths:
                paths.append(parent)
    return paths


def _stdlib_paths() -> list[str]:
    """The standard library only, resolved from sysconfig rather than filtered from sys.path.

    An earlier version of this built the list by taking every `sys.path` entry that was not
    site-packages. That silently handed the child the whole repository, because an editable
    install puts `src/` on `sys.path` and `src/` is not site-packages. The preamble's integrity
    check caught it on the first run — which is the entire reason that check exists, and the
    reason it asserts rather than warns.

    Asking sysconfig for the two stdlib locations cannot pick up project code by accident.
    """
    paths: list[str] = []
    for key in ("stdlib", "platstdlib"):
        location = sysconfig.get_path(key)
        if location and Path(location).exists() and location not in paths:
            paths.append(location)
    # Extension modules (_socket, _json) live beside the stdlib on Windows.
    dlls = Path(sysconfig.get_path("stdlib")).parent / "DLLs"
    if dlls.exists():
        paths.append(str(dlls))
    return paths


def _child_environment() -> dict[str, str]:
    """Built from empty. Only what a Python process needs to start on this OS.

    Deliberately excludes everything else, so there is no credential, no repository path, and no
    `QUANTBOT_*` pointing at the durable ledger or the production locks.
    """
    keep = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS")
    env: dict[str, str] = {name: os.environ[name] for name in keep if name in os.environ}
    # A bare PATH: enough to launch the interpreter, nothing that resolves project tooling.
    system_root = env.get("SYSTEMROOT", "")
    if system_root:
        env["PATH"] = f"{system_root}\\System32;{system_root}"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _collect(outputs: Path, limit: int) -> tuple[Artifact, ...]:
    """Hash everything the child wrote, refusing a run that blew the output budget."""
    collected: list[Artifact] = []
    total = 0
    for path in sorted(outputs.rglob("*")):
        if not path.is_file():
            continue
        size = path.stat().st_size
        total += size
        if total > limit:
            raise SandboxError(f"experiment wrote more than the {limit} byte output budget")
        collected.append(
            Artifact(
                name=str(path.relative_to(outputs)).replace("\\", "/"),
                size_bytes=size,
                sha256=_digest(path),
            )
        )
    return tuple(collected)


def _memory_mb(process: subprocess.Popen[bytes]) -> float | None:
    """Working set of the child AND its descendants, in MB. None when it cannot be determined.

    Summing the tree rather than the direct child is not defensive padding, it is required twice
    over. First, `.venv/Scripts/python.exe` is a trampoline on Windows, so the direct child is a
    ~5MB shim and the interpreter doing the allocating is a grandchild — an earlier version
    measured only the child, reported a flat 4.8MB while the experiment happily allocated 600MB,
    and the limit never fired. Second, issue #12 requires that a script spawning helpers cannot
    escape the ceiling, and only a tree walk sees those.

    Returning None on any failure means the monitor declines to act rather than killing a healthy
    run on a parse error; a false positive destroys a legitimate experiment.
    """
    # Built by concatenation, not str.format: the PowerShell braces would be read as fields.
    script = (
        "$ids=@(" + str(process.pid) + ");$i=0;"
        "while($i -lt $ids.Count){"
        "$kids=Get-CimInstance Win32_Process "
        "-Filter \"ParentProcessId=$($ids[$i])\" -ErrorAction SilentlyContinue;"
        "foreach($k in $kids){if($ids -notcontains $k.ProcessId){$ids+=$k.ProcessId}};$i++};"
        "$t=0;foreach($id in $ids){"
        "$p=Get-CimInstance Win32_Process -Filter \"ProcessId=$id\" "
        "-ErrorAction SilentlyContinue;"
        "if($p){$t+=$p.WorkingSetSize}};[math]::Round($t/1MB,2)"
    )
    try:
        out = subprocess.run(  # noqa: S603 - fixed executable, integer pid interpolated
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=20, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return float(out.splitlines()[-1]) if out else None
    except (ValueError, IndexError):
        return None


class SandboxRunner:
    """Runs one generated script per call, in a workspace it creates and destroys."""

    def __init__(self, policy: SandboxPolicy | None = None) -> None:
        self._policy = policy or SandboxPolicy()

    @property
    def policy(self) -> SandboxPolicy:
        return self._policy

    def _prepare(self, root: Path, source: str, inputs: dict[str, bytes]) -> SandboxPaths:
        paths = SandboxPaths(
            root=root,
            inputs=root / "inputs",
            outputs=root / "outputs",
            script=root / "experiment.py",
        )
        paths.inputs.mkdir(parents=True)
        paths.outputs.mkdir(parents=True)
        for name, payload in inputs.items():
            # Flattened deliberately: a name with separators could escape the inputs directory.
            target = paths.inputs / Path(name).name
            target.write_bytes(payload)
        paths.script.write_text(PREAMBLE + "\n" + source, encoding="utf-8")
        (root / "_sandbox.json").write_text(
            json.dumps(
                {
                    "path": _stdlib_paths() + _approved_package_paths(self._policy),
                    "network": self._policy.network_enabled,
                    "hosts": list(self._policy.resolved_hosts()),
                }
            ),
            encoding="utf-8",
        )
        return paths

    def run(self, source: str, *, inputs: dict[str, bytes] | None = None) -> SandboxResult:
        """Statically check, then execute, then collect. Always cleans up the workspace.

        Raises `StaticCheckError` before anything executes. A script that fails at runtime
        returns a result with `ok=False` instead of raising, because a failed experiment is
        evidence and must be recorded rather than discarded.
        """
        enforce(source, allowed_third_party=self._policy.allowed_third_party)
        script_digest = hashlib.sha256(source.encode("utf-8")).hexdigest()

        root = Path(tempfile.mkdtemp(prefix="elrond-sandbox-"))
        try:
            paths = self._prepare(root, source, inputs or {})
            started = time.monotonic()
            process = subprocess.Popen(  # noqa: S603 - fixed interpreter, constructed argv
                [sys.executable, "-I", "-S", str(paths.script.name)],
                cwd=str(root),
                env=_child_environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            reason = self._supervise(process)
            try:
                out, err = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                out, err = process.communicate()
            duration = time.monotonic() - started

            artifacts = _collect(paths.outputs, self._policy.max_output_bytes)
            return SandboxResult(
                ok=reason is None and process.returncode == 0,
                exit_code=process.returncode,
                stdout=out.decode("utf-8", "replace"),
                stderr=err.decode("utf-8", "replace"),
                duration_seconds=duration,
                artifacts=artifacts,
                terminated_reason=reason,
                script_sha256=script_digest,
            )
        finally:
            # Deterministic cleanup. A leftover workspace could be read by the next experiment.
            shutil.rmtree(root, ignore_errors=True)

    def _supervise(self, process: subprocess.Popen[bytes]) -> str | None:
        """Wait for the child, killing it on wall-clock or memory breach.

        Returns the reason it was stopped, or None if it finished on its own. The kill targets
        the whole process tree because a script that spawned helpers would otherwise outlive it.
        """
        deadline = time.monotonic() + self._policy.wall_clock_seconds
        reason: str | None = None
        while process.poll() is None:
            if time.monotonic() >= deadline:
                reason = "wall-clock"
                break
            used = _memory_mb(process)
            if used is not None and used > self._policy.memory_mb:
                reason = "memory"
                break
            time.sleep(self._policy.poll_interval_seconds)
        if reason is not None:
            self._terminate_tree(process)
        return reason

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
        try:
            subprocess.run(  # noqa: S603 - fixed command, integer pid
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        if process.poll() is None:
            process.kill()
