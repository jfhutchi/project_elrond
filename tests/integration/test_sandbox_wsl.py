"""The OS-enforced sandbox backend, run against a real WSL2 kernel (#21, #12).

These tests are the reason #21 stayed open for weeks rather than being written speculatively.
CLAUDE.md records why: the memory-limit test in #12 accepted either `memory` or `wall-clock`
termination, and that leniency hid a broken monitor for a full run while the suite reported
green. A backend whose tests never execute is the same defect with the leniency moved outward.

So every test here runs the actual thing. If WSL is not present they skip loudly rather than
passing, because a skipped isolation test and a passing one must never look alike.

**Each test carries a control.** Asserting only that the sandboxed read fails would pass against
a backend that failed at everything -- a broken `wsl.exe` invocation refuses to read the ledger
very convincingly. The controls prove the same operation succeeds outside the namespace, which is
the difference between enforcement and coincidence.
"""

from __future__ import annotations

import subprocess

import pytest

from quantbot.sandbox import SandboxPolicy
from quantbot.sandbox.wsl import WslSandbox, wsl_backend_available

pytestmark = pytest.mark.skipif(
    not wsl_backend_available(),
    reason="no WSL distribution with unprivileged namespaces; this suite must not pass without one",
)

#: Generous on purpose. When a memory or filesystem property is under test the wall clock must
#: not be able to cover for it -- that is exactly how #12's memory monitor stayed broken.
ROOMY = SandboxPolicy(wall_clock_seconds=90.0, memory_mb=256)


def _control(command: str) -> subprocess.CompletedProcess[bytes]:
    """Run the same operation in WSL *outside* any namespace, to prove it is otherwise possible."""
    return subprocess.run(
        ["wsl.exe", "-d", "Ubuntu", "-e", "bash", "-lc", command],
        capture_output=True,
        timeout=90,
        check=False,
    )


def test_the_repository_is_absent_even_when_the_exact_path_is_supplied() -> None:
    """#12's outstanding filesystem box, and the whole point of an OS-enforced backend.

    The Windows backend hides the repository by not putting it on `sys.path`. An absolute path
    still resolves there, so a script that knows where to look reads the source, the dotenv and
    the durable ledger. Here the mount namespace removes the Windows drives outright: the file
    does not exist for this process.
    """
    path = "/mnt/e/Documents/GitHub/project_elrond/CLAUDE.md"

    # Control: the file is genuinely readable from WSL, so a failure below means the namespace.
    control = _control(f"head -c 20 {path}")
    assert control.returncode == 0 and control.stdout.strip(), (
        "the control could not read the repository, so this test proves nothing about isolation"
    )

    attack = f"""
import os
try:
    with open({path!r}, "rb") as handle:
        print("READ", len(handle.read(20)))
except OSError as error:
    print("DENIED", type(error).__name__)
print("MNT", sorted(os.listdir("/mnt")))
"""
    result = WslSandbox(ROOMY).run(attack)

    assert "READ" not in result.stdout, f"the sandbox read the repository: {result.stdout}"
    assert "DENIED FileNotFoundError" in result.stdout, result.stdout
    assert "MNT []" in result.stdout, "the Windows drives must not merely be unreadable, but gone"


def test_networking_is_refused_by_the_kernel_and_not_by_a_patched_socket() -> None:
    """#12's outstanding network box.

    The Windows backend replaces `socket.socket`, which generated code can undo by re-importing
    the C module. Here there is no route in the namespace at all, so the attack below deliberately
    reaches for `_socket` directly -- the exact bypass that defeats the in-process guard.
    """
    attack = """
import _socket
try:
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(("1.1.1.1", 53))
    print("CONNECTED")
except OSError as error:
    print("DENIED", type(error).__name__)
"""
    result = WslSandbox(ROOMY).run(attack)

    assert "CONNECTED" not in result.stdout, f"the sandbox reached the network: {result.stdout}"
    assert "DENIED" in result.stdout, result.stdout


def test_the_memory_ceiling_holds_against_a_generous_wall_clock() -> None:
    """The named limit, with every other limit set so it cannot take the credit.

    This is the test CLAUDE.md's engineering standard was written about. It asserts that the
    allocation *cannot succeed*, not that a particular label came back: a script is free to catch
    its own MemoryError, and if it does the ceiling still held. Asserting only the label would
    pass a sandbox that reported nicely and enforced nothing.
    """
    # 256MB ceiling, 90s wall clock. The allocation is four times the ceiling and takes well
    # under a second, so a wall-clock stop here would be a bug in the test, not a pass.
    attack = """
try:
    block = bytearray(1024 * 1024 * 1024)
    print("ALLOCATED", len(block))
except MemoryError:
    print("REFUSED")
"""
    result = WslSandbox(ROOMY).run(attack)

    assert "ALLOCATED" not in result.stdout, f"the ceiling did not hold: {result.stdout}"
    assert "REFUSED" in result.stdout, result.stdout
    assert result.terminated_reason != "wall-clock", (
        "a wall-clock stop would mean this test measured the timeout, not the memory ceiling"
    )
    assert result.duration_seconds < 60, result.duration_seconds


def test_an_infinite_loop_is_killed_by_the_cpu_ceiling_specifically() -> None:
    """RLIMIT_CPU sends SIGKILL. The Windows backend polls and kills at the next interval.

    Asserts `cpu` and not `{"cpu", "wall-clock"}`. Accepting either is what let #12's memory
    monitor stay broken for a full run, and the same leniency here would pass a backend where
    the CPU ceiling never applied and the outer timeout did all the work.

    The subprocess timeout sits fifteen seconds above the CPU ceiling, so a wall-clock stop
    would mean the ceiling genuinely did not fire.
    """
    tight = SandboxPolicy(wall_clock_seconds=20.0, memory_mb=256)

    result = WslSandbox(tight).run("while True:\n    pass\n")

    assert not result.ok
    assert result.terminated_reason == "cpu", (
        f"the CPU ceiling did not fire: {result.terminated_reason} "
        f"after {result.duration_seconds:.1f}s, stderr={result.stderr!r}"
    )
    assert result.duration_seconds < 34, result.duration_seconds


def test_outputs_come_back_and_the_workspace_does_not_survive() -> None:
    """Artifacts cross the boundary; the workspace is destroyed so nothing leaks to the next run."""
    script = """
from pathlib import Path
Path("outputs/factors.txt").write_text("ic=0.031\\n", encoding="utf-8")
print("WROTE")
"""
    result = WslSandbox(ROOMY).run(script)

    assert result.ok, result.stderr
    assert "WROTE" in result.stdout
    assert [item.name for item in result.artifacts] == ["factors.txt"]
    assert result.artifacts[0].size_bytes == len("ic=0.031\n")

    leftovers = _control("ls -d /tmp/elrond-sandbox-* 2>/dev/null | wc -l")
    assert leftovers.stdout.strip() in (b"0", b"0\n"), leftovers.stdout


def test_quantbot_is_not_importable_and_the_broker_is_not_reachable() -> None:
    """The property the Windows backend gets by omission, checked here too.

    Nothing stages `quantbot` into the workspace and the repository is not on any visible
    filesystem, so the broker adapter and every order-submission path are absent rather than
    blocked. Worth asserting on this backend specifically: a mount namespace that accidentally
    kept a path would reintroduce it silently.
    """
    probe = """
import importlib.util
print("QUANTBOT", importlib.util.find_spec("quantbot") is not None)
"""
    result = WslSandbox(ROOMY).run(probe)

    assert "QUANTBOT False" in result.stdout, result.stdout
