"""Execute generated research code in a disposable workspace with no route to the broker.

The enforcement story, in the order it matters:

1. **`sys.path` is built, not inherited.** The child runs the base interpreter with `-I -S`,
   which starts with no site-packages and ignores every `PYTHON*` environment variable. Approved
   scientific distributions are copied into the disposable workspace before launch; source
   package and repository paths are never handed to the child. `quantbot` is never staged, so the
   broker adapter, runtime composition root, and every order-submission path are unreachable —
   not blocked, absent.
2. **The environment is constructed from empty.** Nothing is inherited. There is no `ALPACA_*`
   to read. This is load-bearing because `.env` also sits on disk: hiding variables alone would
   be theatre, but a child with no dotenv loader, no `quantbot`, and no repository path has no
   route to the file.
3. **Data arrives as files.** Inputs are copied in before the child starts, so a normal
   experiment needs no network. Network is opt-in per policy, and when it is on the allowlist is
   enforced rather than merely recorded: only hosts in `policy.resolved_hosts()` resolve, and only
   addresses one of those lookups returned are connectable, so a raw IP has no route. In-process
   enforcement, with the ceiling stated at `PREAMBLE`.
4. **The workspace is disposable and the outputs are quarantined.** Every produced file is
   hashed and recorded so an artifact can name the experiment that made it.

Limits are honest about their strength. Wall-clock is hard, from the subprocess timeout. Memory
is polled by the parent and enforced by killing the process tree, which is real but not
instantaneous. Process count cannot be capped on Windows without a Job Object; grandchildren are
caught by tree termination at the next poll. `policy.py` records this in full.

**Both halves of that were Windows-only until CI ran the suite on Linux.** `_memory_mb` shelled
out to PowerShell and `_terminate_tree` to `taskkill`; neither exists there, so the probe
returned `None` on every poll -- and the monitor is written to decline to act on `None` rather
than kill a healthy run. The memory ceiling was therefore **completely inert on Linux** while
`SandboxPolicy.memory_mb` still read like a control, and the tree kill degraded to killing the
direct child so a script that spawned helpers outlived it.

That was not academic. The runaway-allocation test allocates 20MB a loop; with no ceiling it
reached the kernel OOM killer and took the whole CI job down with SIGTERM before any assertion
ran. The coordinator this project is migrating to is WSL2 Ubuntu, so Linux is the platform that
matters most for it.

POSIX now gets both, and one more thing Windows cannot have: `RLIMIT_AS` is set on the child at
a multiple of the ceiling as a **backstop**. The poll still reports the reason, because a reason
is evidence and a `MemoryError` traceback is not; the rlimit exists so that a child allocating
faster than the poll interval hits a wall the kernel put there instead of the host's swap.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from quantbot.sandbox.policy import SandboxPaths, SandboxPolicy
from quantbot.sandbox.static import enforce

#: Injected ahead of the generated script. Rebuilds a minimal import path, then proves the
#: boundary from the inside: if `quantbot` turns out to be importable the run aborts rather than
#: continuing in a state the caller believes is isolated.
#:
#: The enabled-network branch enforces `policy.resolved_hosts()`. It previously did not: the
#: allowlist was written into `_sandbox.json` and never read, so turning networking on opened
#: every destination while the config file described a restriction that did not exist. A policy
#: field that is written and never consulted is worse than an absent one, because it reads as a
#: control in review.
#:
#: Its strength is the same as the deny branch beside it and no greater: this is in-process
#: monkeypatching, so generated code that deliberately re-imports `_socket` or restores the saved
#: descriptors can step around it. That is #21's job, and it is written down here rather than
#: implied so nobody mistakes it for a boundary against adversarial code. Against the realistic
#: case -- generated research code reaching for a URL nobody approved -- it holds.
PREAMBLE = '''import sys as _sys, json as _json, pathlib as _pathlib
_cfg = _json.loads(_pathlib.Path("_sandbox.json").read_text(encoding="utf-8"))
_sys.path = [p for p in _cfg["path"] if p]
import importlib.util as _u
if _u.find_spec("quantbot") is not None:
    raise SystemExit("SANDBOX INTEGRITY FAILURE: quantbot is importable inside the sandbox")
import socket as _socket
if not _cfg["network"]:
    def _denied(*_a, **_k):
        raise PermissionError("sandbox policy: network access is disabled")
    _socket.socket = _denied          # type: ignore[assignment]
    _socket.create_connection = _denied  # type: ignore[assignment]
else:
    _allowed_hosts = {str(h).lower() for h in _cfg["hosts"]}
    _allowed_addrs = set()
    _real_getaddrinfo = _socket.getaddrinfo
    _real_connect = _socket.socket.connect
    _real_connect_ex = _socket.socket.connect_ex
    def _refuse(_target):
        raise PermissionError(
            "sandbox policy: " + repr(_target) + " is not in the host allowlist"
        )
    def _guarded_getaddrinfo(host, port, *_a, **_k):
        if str(host).lower() not in _allowed_hosts:
            _refuse(host)
        _infos = _real_getaddrinfo(host, port, *_a, **_k)
        for _info in _infos:
            _allowed_addrs.add(_info[4][0])
        return _infos
    def _permitted(_address):
        return (
            isinstance(_address, tuple)
            and len(_address) >= 2
            and _address[0] in _allowed_addrs
        )
    def _guarded_connect(self, address, *_a, **_k):
        if not _permitted(address):
            _refuse(address)
        return _real_connect(self, address, *_a, **_k)
    def _guarded_connect_ex(self, address, *_a, **_k):
        if not _permitted(address):
            _refuse(address)
        return _real_connect_ex(self, address, *_a, **_k)
    _socket.getaddrinfo = _guarded_getaddrinfo
    _socket.socket.connect = _guarded_connect
    _socket.socket.connect_ex = _guarded_connect_ex
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
    #: Highest memory the monitor observed, in MB, or None when it never got a reading -- a
    #: process that exits between polls is measured zero times. None rather than 0.0 for the
    #: usual reason: an unmeasured figure that reads as a measured zero is worse than a gap.
    peak_memory_mb: float | None = None
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


_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")
_UNSAFE_STAGING_SUFFIXES = frozenset({".egg-link", ".pth", ".pyc", ".pyo"})
_PACKAGE_COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.pyo", "*.pth", "*.egg-link", "__editable__*"
)


def _source_repository_root() -> Path | None:
    """Return the checkout containing this module, if the package is running from one."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "src" / "quantbot" / "sandbox" / "runner.py"
        ).is_file():
            return candidate.resolve()
    return None


def _assert_child_paths_outside_repository(paths: list[str]) -> None:
    """Fail closed before serialising any repository path into the child's import path."""
    repository = _source_repository_root()
    if repository is None:
        return
    disclosed = [
        path for path in paths if Path(path).resolve(strict=False).is_relative_to(repository)
    ]
    if disclosed:
        raise SandboxError("child sys.path entry is inside the repository root")


def _distribution_closure(package_names: tuple[str, ...]) -> list[importlib.metadata.Distribution]:
    """Resolve installed distributions needed by the approved import roots.

    Optional-extra requirements are excluded. Installed conditional requirements are safe to
    include: they are copied into the workspace and disclose no source location to the child.
    """
    import_to_distributions = importlib.metadata.packages_distributions()
    pending = [
        distribution
        for package_name in package_names
        for distribution in import_to_distributions.get(package_name, ())
    ]
    resolved: list[importlib.metadata.Distribution] = []
    seen: set[str] = set()
    while pending:
        distribution_name = pending.pop()
        normalized = distribution_name.lower().replace("_", "-")
        if normalized in seen:
            continue
        try:
            distribution = importlib.metadata.distribution(distribution_name)
        except importlib.metadata.PackageNotFoundError:
            continue
        seen.add(normalized)
        resolved.append(distribution)
        for requirement in distribution.requires or ():
            marker = requirement.partition(";")[2]
            if "extra" in marker:
                continue
            match = _REQUIREMENT_NAME.match(requirement)
            if match is not None:
                pending.append(match.group(0))
    return resolved


def _distribution_import_roots(
    distributions: list[importlib.metadata.Distribution],
) -> list[str]:
    """Map the resolved dependency closure back to import roots for editable installs."""
    selected: set[str] = set()
    for distribution in distributions:
        distribution_name = distribution.metadata["Name"]
        selected.add(distribution_name.lower().replace("_", "-"))
    import_roots: list[str] = []
    for import_root, owners in importlib.metadata.packages_distributions().items():
        normalized_owners = {owner.lower().replace("_", "-") for owner in owners}
        if not normalized_owners.intersection(selected):
            continue
        unselected = normalized_owners.difference(selected)
        if unselected:
            raise SandboxError(
                f"import root {import_root!r} is shared with distributions outside "
                "the approved dependency closure"
            )
        import_roots.append(import_root)
    return import_roots


def _is_unsafe_staged_path(relative: Path) -> bool:
    """Reject files that preserve or activate locations from the parent installation."""
    if relative.is_absolute() or ".." in relative.parts:
        return True
    for part in relative.parts:
        lowered = part.lower()
        if lowered == "__pycache__" or lowered.startswith("__editable__"):
            return True
        if Path(lowered).suffix in _UNSAFE_STAGING_SUFFIXES:
            return True
    return relative.name.lower() == "direct_url.json"


def _copy_distribution(
    distribution: importlib.metadata.Distribution, destination: Path
) -> None:
    """Copy one installed distribution without source-location metadata or external scripts."""
    for entry in distribution.files or ():
        relative = Path(str(entry))
        if _is_unsafe_staged_path(relative):
            continue
        source = Path(str(distribution.locate_file(entry)))
        if not source.is_file():
            continue
        target = (destination / relative).resolve(strict=False)
        if not target.is_relative_to(destination.resolve()):
            raise SandboxError("package staging target escaped the sandbox workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _copy_import_root(name: str, destination: Path) -> bool:
    """Copy an approved import root, including adjacent native-library directories."""
    if not name.isidentifier():
        raise SandboxError(f"invalid approved package name: {name!r}")
    spec = importlib.util.find_spec(name)
    if spec is None:
        return False
    copied = False
    for location_text in spec.submodule_search_locations or ():
        location = Path(location_text)
        if not location.is_dir():
            continue
        shutil.copytree(
            location,
            destination / name,
            dirs_exist_ok=True,
            ignore=_PACKAGE_COPY_IGNORE,
        )
        for native_libraries in location.parent.glob(f"{location.name}*.libs"):
            if native_libraries.is_dir():
                shutil.copytree(
                    native_libraries,
                    destination / native_libraries.name,
                    dirs_exist_ok=True,
                    ignore=_PACKAGE_COPY_IGNORE,
                )
        copied = True
    origin = spec.origin
    if not copied and origin is not None and origin not in ("built-in", "frozen"):
        source = Path(origin)
        if source.is_file() and not _is_unsafe_staged_path(Path(source.name)):
            shutil.copy2(source, destination / source.name)
            copied = True
    return copied


def _approved_package_paths(policy: SandboxPolicy, destination: Path) -> list[str]:
    """Stage approved packages into the disposable workspace, never exposing source paths.

    Resolution happens only in the trusted parent. Missing packages remain omitted so the child
    receives the same clear import failure as before. Distribution metadata supplies transitive
    runtime dependencies, while copying each approved import root also supports editable or
    metadata-free packages without carrying their original path across the boundary.
    """
    destination.mkdir(parents=True, exist_ok=True)
    distributions = _distribution_closure(policy.allowed_third_party)
    for distribution in distributions:
        _copy_distribution(distribution, destination)
    import_roots = dict.fromkeys(
        (*policy.allowed_third_party, *_distribution_import_roots(distributions))
    )
    copied = False
    for name in import_roots:
        copied = _copy_import_root(name, destination) or copied
    if not copied and not any(destination.iterdir()):
        destination.rmdir()
        return []
    return [str(destination.resolve())]


def _stdlib_paths() -> list[str]:
    """The base interpreter's standard library, never the repository-local virtualenv.

    An earlier version of this built the list by taking every `sys.path` entry that was not
    site-packages. That silently handed the child the whole repository, because an editable
    install puts `src/` on `sys.path` and `src/` is not site-packages. The preamble's integrity
    check caught it on the first run — which is the entire reason that check exists, and the
    reason it asserts rather than warns.

    `platstdlib` under a virtualenv can itself point into `<repo>/.venv/Lib`, so every sysconfig
    base is explicitly replaced with the base interpreter prefix before paths are resolved.
    """
    base_variables = {
        "base": sys.base_prefix,
        "platbase": sys.base_exec_prefix,
        "installed_base": sys.base_prefix,
        "installed_platbase": sys.base_exec_prefix,
    }
    scheme = sysconfig.get_preferred_scheme("prefix")
    paths: list[str] = []
    for key in ("stdlib", "platstdlib"):
        location = sysconfig.get_path(key, scheme=scheme, vars=base_variables)
        if location and Path(location).exists() and location not in paths:
            paths.append(location)
    for extensions in (Path(sys.base_prefix) / "DLLs", Path(paths[0]) / "lib-dynload"):
        if extensions.exists() and str(extensions) not in paths:
            paths.append(str(extensions))
    return paths


def _child_interpreter() -> str:
    """Use the base executable so interpreter metadata cannot disclose a repo-local venv."""
    executable = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    repository = _source_repository_root()
    if repository is not None and executable.is_relative_to(repository):
        raise SandboxError("child interpreter is inside the repository root")
    if not executable.is_file():
        raise SandboxError(f"base Python interpreter does not exist: {executable}")
    return str(executable)


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


#: How much slack the kernel ceiling gets over the polled one. The poll is meant to fire first
#: and report `memory`; this only catches a child allocating faster than `poll_interval_seconds`.
#: Too tight and a legitimate spike between polls dies with a MemoryError instead of a reason;
#: too loose and the host absorbs the difference.
_RLIMIT_HEADROOM = 2


def _posix_child_limits(memory_mb: int) -> Callable[[], None]:
    """Return a `preexec_fn` applying an address-space ceiling to the child, on POSIX only.

    Soft *and* hard are set together. A process may raise its own soft limit up to the hard one,
    so setting the soft limit alone leaves the generated script holding the knob -- which is the
    same shape as a sandbox whose escape is `importlib.reload`.

    The `sys.platform` guard is what lets mypy check this on Linux and skip it on Windows. The
    defect this whole change is about was a Windows-only implementation nobody type-checked on
    the other platform, and writing the replacement so that only one platform checks it would
    reproduce that exactly.
    """
    if sys.platform == "win32":
        raise RuntimeError("address-space limits are a POSIX facility")
    else:
        import resource

        ceiling = memory_mb * _RLIMIT_HEADROOM * 1024 * 1024

        def apply() -> None:  # pragma: no cover - runs between fork and exec in the child
            resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
            os.setsid()

        return apply


def _posix_memory_mb(pid: int) -> float | None:
    """Resident set of a process and its descendants, in MB, read from `/proc`.

    `/proc` rather than a helper binary: `ps` output is locale- and platform-shaped, and this
    probe returning `None` is indistinguishable from a healthy run, which is exactly how the
    ceiling came to be inert here in the first place.
    """
    if sys.platform == "win32":
        raise RuntimeError("/proc is a POSIX facility")
    else:
        children: dict[int, list[int]] = {}
        resident: dict[int, float] = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
                # The comm field is parenthesised and may itself contain spaces, so split on
                # the last ") " rather than tokenising the whole line.
                fields = stat.rsplit(") ", 1)
                if len(fields) != 2:
                    continue
                columns = fields[1].split()
                parent = int(columns[1])
                # Column 23 of /proc/pid/stat counting from the field after the comm, in pages.
                pages = int(columns[21])
            except (OSError, ValueError, IndexError):
                continue
            children.setdefault(parent, []).append(int(entry.name))
            resident[int(entry.name)] = pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)

        if pid not in resident:
            return None
        total = 0.0
        frontier = [pid]
        seen: set[int] = set()
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            total += resident.get(current, 0.0)
            frontier.extend(children.get(current, ()))
        return round(total, 2)


def _memory_mb(process: subprocess.Popen[bytes]) -> float | None:
    """Working set of the child AND its descendants, in MB. None when it cannot be determined.

    Summing the tree rather than the direct child is not defensive padding, it is required twice
    over. First, `.venv/Scripts/python.exe` is a trampoline on Windows, so the direct child is a
    ~5MB shim and the interpreter doing the allocating is a grandchild — an earlier version
    measured only the child, reported a flat 4.8MB while the experiment happily allocated 600MB,
    and the limit never fired. Second, issue #12 requires that a script spawning helpers cannot
    escape the ceiling, and only a tree walk sees those.

    Returning None on any failure means the monitor declines to act rather than killing a healthy
    run on a parse error; a false positive destroys a legitimate experiment. The cost of that
    choice is that a probe which never works looks exactly like a healthy run, which is what
    happened on Linux for as long as this function was PowerShell and nothing else.
    """
    if sys.platform != "win32":
        return _posix_memory_mb(process.pid)
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

    #: Concealment, not containment -- see the module docstring. An absolute path still resolves
    #: here and `importlib.reload(socket)` undoes the network guard, so callers whose safety
    #: depends on the kernel (anomaly mining, model-authored experiment code) must refuse this
    #: backend. Stated as a property because no configuration can change the answer: a caller
    #: that had to infer it from the class name would eventually infer wrong.
    os_enforced = False

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
        child_paths = _stdlib_paths() + _approved_package_paths(
            self._policy, root / "_packages"
        )
        _assert_child_paths_outside_repository(child_paths)
        (root / "_sandbox.json").write_text(
            json.dumps(
                {
                    "path": child_paths,
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
                [_child_interpreter(), "-I", "-S", str(paths.script.name)],
                cwd=str(root),
                env=_child_environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=(  # noqa: PLW1509 - the point is to run before exec, in the child
                    None
                    if sys.platform == "win32"
                    else _posix_child_limits(self._policy.memory_mb)
                ),
            )
            reason, peak_memory = self._supervise(process)
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
                peak_memory_mb=peak_memory,
                artifacts=artifacts,
                terminated_reason=reason,
                script_sha256=script_digest,
            )
        finally:
            # Deterministic cleanup. A leftover workspace could be read by the next experiment.
            shutil.rmtree(root, ignore_errors=True)

    def _supervise(self, process: subprocess.Popen[bytes]) -> tuple[str | None, float | None]:
        """Wait for the child, killing it on wall-clock or memory breach.

        Returns the reason it was stopped -- None if it finished on its own -- and the highest
        memory reading taken. The peak is kept because the ceiling is configuration and the peak
        is evidence: a panel showing `memory_mb` would be displaying its own settings back, and a
        run that used 40MB of a 1024MB allowance is a different fact from one that used 1000.

        The kill targets the whole process tree because a script that spawned helpers would
        otherwise outlive it.
        """
        deadline = time.monotonic() + self._policy.wall_clock_seconds
        reason: str | None = None
        peak: float | None = None
        while process.poll() is None:
            if time.monotonic() >= deadline:
                reason = "wall-clock"
                break
            used = _memory_mb(process)
            if used is not None:
                peak = used if peak is None else max(peak, used)
                if used > self._policy.memory_mb:
                    reason = "memory"
                    break
            time.sleep(self._policy.poll_interval_seconds)
        if reason is not None:
            self._terminate_tree(process)
        return reason, peak

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
        """Kill the child and everything it started, on both platforms.

        On POSIX the child is given its own session by `_posix_child_limits`, so the process
        group is the tree and `killpg` reaches all of it. Falling through to `process.kill()`
        alone -- which is what happened here before, because `taskkill` does not exist on Linux
        -- kills the direct child and leaves its helpers running against the same ceiling the
        run was stopped for.
        """
        if sys.platform != "win32":
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            if process.poll() is None:
                process.kill()
            return
        try:
            subprocess.run(  # noqa: S603 - fixed command, integer pid
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        if process.poll() is None:
            process.kill()
