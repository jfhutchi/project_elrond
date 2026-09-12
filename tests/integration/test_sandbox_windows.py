"""The Windows OS-enforced backend, run against a real AppContainer and Job Object (#21, #12).

Written to the same standard as `test_sandbox_wsl.py`, for the same reason CLAUDE.md records: the
memory-limit test in #12 accepted either `memory` or `wall-clock` termination, and that leniency
hid a broken monitor for a full run while the suite reported green. A sandbox whose tests never
execute is that defect with the leniency moved outward.

So every test here runs the actual thing. If the backend cannot be established they skip loudly
rather than passing, because a skipped isolation test and a passing one must never look alike.

**Each test carries a control.** Asserting only that the sandboxed read fails would pass against a
backend that failed at everything -- a `CreateProcess` that never launched refuses to read the
ledger very convincingly. The controls prove the same operation succeeds outside the container, or
succeeds inside it under a wider limit, which is the difference between enforcement and a
coincidence that happens to look like one.

**Two tests deliberately evade the static scanner**, and say so in their names. `static.py` is
documented as defence in depth and trivially bypassable; the property this backend actually claims
is that the kernel holds even when that scan does not. A test that only ever submitted
scanner-clean code would be measuring the scanner.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from quantbot.sandbox import (
    SandboxPolicy,
    SandboxRunner,
    select_isolated_backend,
    windows_backend_available,
    wsl_backend_available,
)
from quantbot.sandbox.windows import SANDBOX_ROOT, WindowsSandbox, WindowsUnavailable

pytestmark = pytest.mark.skipif(
    not windows_backend_available(),
    reason=(
        "no Windows AppContainer backend on this host; this suite must not pass without one"
    ),
)

#: Generous on purpose. When a filesystem, network or memory property is under test the wall clock
#: must not be able to cover for it -- that is exactly how #12's memory monitor stayed broken.
ROOMY = SandboxPolicy(wall_clock_seconds=90.0, memory_mb=256)


def _sandbox(policy: SandboxPolicy | None = None) -> WindowsSandbox:
    return WindowsSandbox(policy or ROOMY)


# --- execution and capture ----------------------------------------------------------------------


def test_a_trivial_script_runs_and_its_stdout_is_captured() -> None:
    result = _sandbox().run("print('experiment produced this')\n")

    assert result.ok
    assert result.exit_code == 0
    assert "experiment produced this" in result.stdout
    assert result.terminated_reason is None


def test_stderr_is_captured_separately_from_stdout() -> None:
    result = _sandbox().run(
        "import sys\nprint('to out')\nprint('to err', file=sys.stderr)\n"
    )

    assert "to out" in result.stdout
    assert "to err" not in result.stdout, "stderr was merged into stdout"
    assert "to err" in result.stderr


def test_a_nonzero_exit_is_reported_as_evidence_rather_than_raised() -> None:
    """A failed experiment is a result, not a sandbox failure. It must come back, not blow up."""
    result = _sandbox().run("raise SystemExit(3)\n")

    assert result.exit_code == 3
    assert result.ok is False
    assert result.terminated_reason is None, "the sandbox did not stop this; the script exited"


def test_an_uncaught_exception_reports_its_traceback_and_a_failing_exit() -> None:
    result = _sandbox().run("raise ValueError('deliberate')\n")

    assert result.ok is False
    assert result.exit_code != 0
    assert "ValueError: deliberate" in result.stderr


def test_artifacts_are_collected_and_content_addressed() -> None:
    import hashlib

    payload = b"a,b\n1,2\n"
    # Bytes rather than text: `write_text` would translate the newlines on this platform and the
    # assertion below would then be measuring Windows line endings instead of the collector.
    result = _sandbox().run(
        "import pathlib\n"
        "pathlib.Path('outputs').mkdir(exist_ok=True)\n"
        f"pathlib.Path('outputs/series.csv').write_bytes({payload!r})\n"
    )

    assert result.ok, result.stderr
    assert [artifact.name for artifact in result.artifacts] == ["series.csv"]
    assert result.artifacts[0].size_bytes == len(payload)
    assert result.artifacts[0].sha256 == hashlib.sha256(payload).hexdigest()


def test_the_script_hash_names_the_code_that_actually_ran() -> None:
    import hashlib

    source = "print('reproducible')\n"
    result = _sandbox().run(source)

    assert result.script_sha256 == hashlib.sha256(source.encode("utf-8")).hexdigest()


def test_peak_memory_is_measured_by_the_kernel_rather_than_polled() -> None:
    """A run that lived entirely between two polls still has to be accounted for.

    The control is the ordering: a script that allocates must report a higher peak than one that
    does not. Asserting only "not None" would pass against a field wired to a constant.
    """
    small = _sandbox().run("print('idle')\n")
    large = _sandbox().run("block = bytearray(64 * 1024 * 1024)\nprint(len(block))\n")

    assert small.peak_memory_mb is not None
    assert large.peak_memory_mb is not None
    assert large.peak_memory_mb > small.peak_memory_mb + 32, (
        f"a 64MB allocation was not visible in the job accounting "
        f"({small.peak_memory_mb} -> {large.peak_memory_mb})"
    )


# --- filesystem containment ---------------------------------------------------------------------


def test_the_repository_is_denied_even_when_the_exact_path_is_supplied() -> None:
    """#12's outstanding filesystem box, and the whole reason `os_enforced` exists.

    `SandboxRunner` hides the repository by keeping it off `sys.path`. An absolute path still
    resolves there, which is precisely why `mine_for_anomalies` refuses that backend. Here the
    AppContainer token fails the access check, so the path does not resolve however it was built.
    """
    target = Path(__file__).resolve().parents[2] / "pyproject.toml"

    # Control: the file is genuinely readable by this user, so a failure below is the container.
    assert target.read_bytes()[:10], (
        "the control could not read the repository; this proves nothing"
    )

    result = _sandbox().run(
        "import pathlib\n"
        f"target = pathlib.Path({str(target)!r})\n"
        "try:\n"
        "    data = target.read_bytes()\n"
        "    print('READ', len(data))\n"
        "except OSError as error:\n"
        "    print('DENIED', type(error).__name__)\n"
    )

    assert result.ok, result.stderr
    assert "DENIED" in result.stdout, f"the sandbox read the repository: {result.stdout!r}"
    assert "READ" not in result.stdout


def test_the_user_profile_cannot_be_listed() -> None:
    """Where the durable store, the dotenv and the production locks live on this host."""
    profile = Path.home()

    # Control: this user can list their own profile.
    assert os.listdir(profile), "the control could not list the profile; this proves nothing"

    result = _sandbox().run(
        "import os\n"
        f"try:\n"
        f"    print('LISTED', len(os.listdir({str(profile)!r})))\n"
        "except OSError as error:\n"
        "    print('DENIED', type(error).__name__)\n"
    )

    assert result.ok, result.stderr
    assert "DENIED" in result.stdout, f"the sandbox listed the user profile: {result.stdout!r}"


def test_the_deployed_runtime_checkout_is_unreachable() -> None:
    """The paper coordinator's own checkout, named explicitly because it is the one that matters.

    Skips rather than passes when the checkout is absent: a denial for a path that does not exist
    is not evidence of containment.
    """
    runtime = Path.home() / "elrond-omen-runtime" / "pyproject.toml"
    if not runtime.is_file():
        pytest.skip("the deployed runtime checkout is not present on this host")

    assert runtime.read_bytes()[:10], "the control could not read it; this proves nothing"

    result = _sandbox().run(
        "import pathlib\n"
        f"try:\n"
        f"    print('READ', len(pathlib.Path({str(runtime)!r}).read_bytes()))\n"
        "except OSError as error:\n"
        "    print('DENIED', type(error).__name__)\n"
    )

    assert "DENIED" in result.stdout, f"the sandbox read the deployed runtime: {result.stdout!r}"


def test_the_workspace_itself_remains_writable() -> None:
    """The control for every denial above: the child is not simply broken at all file access."""
    result = _sandbox().run(
        "import pathlib\n"
        "pathlib.Path('scratch.txt').write_text('written')\n"
        "print('WROTE', pathlib.Path('scratch.txt').read_text())\n"
    )

    assert result.ok, result.stderr
    assert "WROTE written" in result.stdout


# --- network denial -------------------------------------------------------------------------------


def test_network_is_refused_by_the_kernel_and_survives_reimporting_socket() -> None:
    """The difference between this backend and `SandboxRunner`'s monkeypatched `socket`.

    `runner.py` says so itself: generated code that re-imports `_socket` steps around its guard.
    Here there is no guard to step around -- the AppContainer has no `internetClient` capability,
    so the Windows Filtering Platform refuses in the kernel. Reloading the module changes nothing,
    and that reload is the assertion that separates the two designs.

    The in-sandbox control is the final write: the interpreter is alive and only egress is denied.
    """
    result = _sandbox().run(
        "import importlib, pathlib, socket\n"
        "def attempt(label):\n"
        "    try:\n"
        "        socket.create_connection(('1.1.1.1', 80), timeout=5).close()\n"
        "        print(label, 'CONNECTED')\n"
        "    except OSError as error:\n"
        "        print(label, 'DENIED', type(error).__name__)\n"
        "attempt('first')\n"
        "importlib.reload(socket)\n"
        "attempt('after-reload')\n"
        "try:\n"
        "    socket.getaddrinfo('example.com', 443)\n"
        "    print('dns RESOLVED')\n"
        "except OSError as error:\n"
        "    print('dns DENIED', type(error).__name__)\n"
        "pathlib.Path('alive.txt').write_text('yes')\n"
        "print('CONTROL alive')\n"
    )

    assert result.ok, result.stderr
    assert "first DENIED" in result.stdout
    assert "after-reload DENIED" in result.stdout, (
        "reloading socket restored egress, so the denial was in-process rather than kernel-enforced"
    )
    assert "dns DENIED" in result.stdout
    assert "CONNECTED" not in result.stdout
    assert "CONTROL alive" in result.stdout, "the child was broken, so the denials prove nothing"


def test_a_network_enabled_policy_is_refused_at_construction() -> None:
    """Fail closed. The only switch available grants all egress, not the policy's allowlist."""
    with pytest.raises(WindowsUnavailable, match="does not offer network access"):
        WindowsSandbox(SandboxPolicy(wall_clock_seconds=30.0, network_enabled=True))


# --- resource ceilings ------------------------------------------------------------------------


def test_the_memory_ceiling_refuses_the_allocation_itself() -> None:
    """Asserts `memory` specifically, against a deliberately generous wall clock.

    CLAUDE.md was written from the version of this test that accepted `memory` or `wall-clock`.
    The wall clock here is 90 seconds and the allocation reaches its ceiling in well under one, so
    a wall-clock termination would mean the memory ceiling did nothing.

    The control is the second run: the identical script under a larger ceiling completes. Without
    it, a script that failed for its own reasons would look exactly like a contained one.
    """
    script = (
        "block = []\n"
        "for _ in range(60):\n"
        "    block.append(bytearray(8 * 1024 * 1024))\n"
        "print('ALLOCATED', len(block) * 8)\n"
    )

    contained = WindowsSandbox(SandboxPolicy(wall_clock_seconds=90.0, memory_mb=128)).run(script)

    assert contained.terminated_reason == "memory", (
        f"expected the memory ceiling to stop this, got {contained.terminated_reason!r} "
        f"after {contained.duration_seconds:.1f}s: {contained.stderr[-300:]!r}"
    )
    assert contained.ok is False
    assert "ALLOCATED" not in contained.stdout
    assert contained.duration_seconds < 60, "this was stopped by the clock, not by the ceiling"

    # Control: the same script, more room, finishes. So the failure above was the ceiling.
    allowed = WindowsSandbox(SandboxPolicy(wall_clock_seconds=90.0, memory_mb=1024)).run(script)
    assert allowed.ok, f"the control run failed, so the test above proves nothing: {allowed.stderr}"
    assert "ALLOCATED 480" in allowed.stdout


def test_the_wall_clock_terminates_a_script_that_will_not_stop() -> None:
    """Asserts `wall-clock` specifically, with memory set high enough that it cannot interfere."""
    result = WindowsSandbox(SandboxPolicy(wall_clock_seconds=3.0, memory_mb=1024)).run(
        "while True:\n    pass\n"
    )

    assert result.terminated_reason == "wall-clock", (
        f"expected the deadline to stop this, got {result.terminated_reason!r}"
    )
    assert result.ok is False
    assert result.duration_seconds < 30, "termination did not happen near the deadline"

    # Control: a script that finishes inside the same deadline is not touched.
    quick = WindowsSandbox(SandboxPolicy(wall_clock_seconds=3.0, memory_mb=1024)).run(
        "print('done')\n"
    )
    assert quick.ok and quick.terminated_reason is None


def test_the_process_ceiling_refuses_a_fork_bomb_by_evading_the_static_scanner() -> None:
    """`ActiveProcessLimit` is the limit `policy.py` recorded as impossible without a Job Object.

    The script reaches `subprocess` through `importlib` because the static scan forbids importing
    it directly. That evasion is the point: the scan is defence in depth, and what is under test
    is the kernel ceiling that holds when the scan is bypassed.

    The control is the first spawn succeeding. A job that refused every process would pass a test
    that only asserted "the bomb did not run".
    """
    result = WindowsSandbox(SandboxPolicy(wall_clock_seconds=60.0, memory_mb=512)).run(
        "import importlib, sys\n"
        "subprocess = importlib.import_module('subprocess')\n"
        "spawned = []\n"
        "for index in range(12):\n"
        "    try:\n"
        "        spawned.append(subprocess.Popen([sys.executable, '-I', '-S', '-c',"
        " 'import time; time.sleep(30)']))\n"
        "    except OSError as error:\n"
        "        print('REFUSED at', index, type(error).__name__)\n"
        "        break\n"
        "else:\n"
        "    print('NEVER REFUSED', len(spawned))\n"
        "print('SPAWNED', len(spawned))\n"
    )

    assert "NEVER REFUSED" not in result.stdout, (
        f"the job permitted twelve processes, so the ceiling is not enforced: {result.stdout!r}"
    )
    assert "REFUSED at" in result.stdout, (
        f"expected a kernel refusal: {result.stdout!r} {result.stderr[-300:]!r}"
    )
    # Control: at least one child actually started, so the refusal is a ceiling and not a
    # container that cannot spawn anything at all.
    assert "SPAWNED 0" not in result.stdout, "no process started at all, so no ceiling was tested"


def test_descendants_do_not_outlive_a_terminated_run() -> None:
    """Tree termination, asserted on a process that was demonstrably alive first.

    `KILL_ON_JOB_CLOSE` makes this atomic rather than best-effort, which is the upgrade over
    `taskkill /T` at the next poll. The child prints the grandchild's pid before hanging, so the
    assertion runs against a pid this test watched get created rather than a hypothetical one.
    """
    result = WindowsSandbox(SandboxPolicy(wall_clock_seconds=5.0, memory_mb=512)).run(
        "import importlib, sys, time\n"
        "subprocess = importlib.import_module('subprocess')\n"
        "child = subprocess.Popen([sys.executable, '-I', '-S', '-c',"
        " 'import time; time.sleep(120)'])\n"
        "print('GRANDCHILD', child.pid, flush=True)\n"
        "time.sleep(120)\n"
    )

    assert result.terminated_reason == "wall-clock"
    marker = [line for line in result.stdout.splitlines() if line.startswith("GRANDCHILD")]
    assert marker, f"the grandchild never started, so nothing was tested: {result.stderr[-300:]!r}"
    pid = int(marker[0].split()[1])

    # The grandchild asked to sleep two minutes and the run was stopped after three seconds, so
    # anything still holding this pid outlived its job.
    listing = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert str(pid) not in listing.stdout, (
        f"a descendant survived termination of its job: {listing.stdout!r}"
    )


def test_a_descendant_dies_even_when_the_script_exits_cleanly() -> None:
    """`KILL_ON_JOB_CLOSE` specifically, which the terminated-run test above does not reach.

    That test passes on `TerminateJobObject` alone -- removing `KILL_ON_JOB_CLOSE` leaves it
    green, which is how this gap was found. Here the script exits 0 and nothing calls terminate,
    so the only thing that can reap the grandchild is closing the job handle. A backgrounded
    process outliving a *successful* experiment is the realistic leak: nothing looks wrong, and
    the host accumulates sleepers.
    """
    result = WindowsSandbox(SandboxPolicy(wall_clock_seconds=30.0, memory_mb=512)).run(
        "import importlib, sys\n"
        "subprocess = importlib.import_module('subprocess')\n"
        "child = subprocess.Popen([sys.executable, '-I', '-S', '-c',"
        " 'import time; time.sleep(120)'])\n"
        "print('GRANDCHILD', child.pid, flush=True)\n"
    )

    assert result.ok, f"the script itself failed, so nothing was tested: {result.stderr[-300:]!r}"
    assert result.terminated_reason is None, "this run must finish on its own, not be stopped"
    marker = [line for line in result.stdout.splitlines() if line.startswith("GRANDCHILD")]
    assert marker, f"the grandchild never started: {result.stderr[-300:]!r}"
    pid = int(marker[0].split()[1])

    listing = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert str(pid) not in listing.stdout, (
        f"a descendant outlived a cleanly-exiting experiment: {listing.stdout!r}"
    )


# --- environment and integrity ------------------------------------------------------------------


def test_no_credential_reaches_the_child_even_when_the_scanner_is_evaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The environment is built from empty, so there is nothing to filter and nothing to miss.

    `getattr(os, 'environ')` gets past the static scan, which forbids the attribute by name. That
    is deliberate here for the same reason as the fork-bomb test: the claim is about the child's
    environment actually being empty, not about the scanner catching the read.
    """
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "leak-canary-key")
    monkeypatch.setenv("QUANTBOT_DATABASE_URL", "leak-canary-url")

    # Control: the canaries really are in this process's environment.
    assert os.environ["ALPACA_PAPER_API_KEY"] == "leak-canary-key"

    result = _sandbox().run(
        "import os\n"
        "environment = getattr(os, 'envi' + 'ron')\n"
        "print('NAMES', sorted(environment))\n"
        "print('CANARIES', [k for k in environment if 'ALPACA' in k or 'QUANTBOT' in k])\n"
    )

    assert result.ok, result.stderr
    assert "leak-canary" not in result.stdout
    assert "CANARIES []" in result.stdout, f"a credential reached the sandbox: {result.stdout!r}"


def test_the_child_receives_exactly_the_variables_the_backend_constructs() -> None:
    """Pins the whole set, so a variable added later is a decision rather than an accident.

    Named individually rather than checked for a few bad prefixes: a denylist of `ALPACA_*` would
    keep passing the day something adds `QUANTBOT_DB_PATH` to the child, and the environment being
    *built from empty* is the property worth guarding.

    `LOCALAPPDATA` is in the set because `CreateProcessW` refuses the launch without it. Note what
    this test does **not** claim: the values still contain the host username, because the workspace
    lives under the user profile and the child has to know its own working directory. That is host
    shape, not host contents, and `docs/sandbox-contract.md` records it.
    """
    result = _sandbox().run(
        "import os\n"
        "environment = getattr(os, 'envi' + 'ron')\n"
        "print('NAMES', ','.join(sorted(environment)))\n"
    )

    assert result.ok, result.stderr
    names = {
        name
        for line in result.stdout.splitlines()
        if line.startswith("NAMES ")
        for name in line[len("NAMES ") :].split(",")
        if name
    }
    assert names == {
        "COMSPEC",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "PATH",
        "PATHEXT",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONIOENCODING",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    }, f"the child environment changed shape: {sorted(names)}"


def test_quantbot_is_not_importable_inside_the_sandbox() -> None:
    """The preamble integrity check. If `quantbot` is ever found, the run aborts, not continues."""
    # Control: the package is importable in this process, so its absence below is the sandbox.
    import quantbot  # noqa: F401

    result = _sandbox().run(
        "import importlib.util\n"
        "print('FOUND' if importlib.util.find_spec('quant' + 'bot') else 'ABSENT')\n"
    )

    assert result.ok, result.stderr
    assert "ABSENT" in result.stdout


def test_the_workspace_is_destroyed_after_the_run() -> None:
    runs = SANDBOX_ROOT / "runs"
    before = set(runs.iterdir()) if runs.is_dir() else set()

    result = _sandbox().run("import pathlib\npathlib.Path('leftover.bin').write_bytes(b'x' * 64)\n")
    assert result.ok, result.stderr

    after = set(runs.iterdir()) if runs.is_dir() else set()
    assert after <= before, f"the run left a workspace behind: {sorted(after - before)}"


def test_a_workspace_is_removed_even_when_the_script_is_terminated() -> None:
    runs = SANDBOX_ROOT / "runs"
    before = set(runs.iterdir()) if runs.is_dir() else set()

    result = WindowsSandbox(SandboxPolicy(wall_clock_seconds=3.0, memory_mb=512)).run(
        "while True:\n    pass\n"
    )
    assert result.terminated_reason == "wall-clock"

    after = set(runs.iterdir()) if runs.is_dir() else set()
    assert after <= before, f"a terminated run left a workspace behind: {sorted(after - before)}"


# --- backend selection ------------------------------------------------------------------------


def test_the_backend_declares_itself_os_enforced() -> None:
    """The single bit `mine_for_anomalies` and `codegen` gate on."""
    assert WindowsSandbox.os_enforced is True
    assert SandboxRunner.os_enforced is False


def test_the_selector_chooses_an_os_enforced_backend_on_this_host() -> None:
    selected = select_isolated_backend(ROOMY)

    assert selected.os_enforced is True
    assert not isinstance(selected, SandboxRunner)
    if not wsl_backend_available():
        assert isinstance(selected, WindowsSandbox), (
            "WSL is unavailable here, so the Windows backend is the only one that can be chosen"
        )


@pytest.mark.skipif(sys.platform != "win32", reason="the staging path is Windows-only")
def test_the_staged_interpreter_is_not_inside_the_repository() -> None:
    """A runtime staged inside the checkout would hand the child the thing it must not reach."""
    repository = Path(__file__).resolve().parents[2]

    assert not SANDBOX_ROOT.resolve().is_relative_to(repository)
