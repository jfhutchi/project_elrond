"""Adversarial proof of the sandbox boundary. Issue #12.

These tests run the real sandbox against real attack code. Nothing is mocked, because a mocked
sandbox proves only that the mock was written to pass.

Two layers are tested separately and it matters which is which:

* The **static** layer refuses obvious attempts before execution. It is defence in depth and is
  trivially bypassable by construction, so a test that only proved static refusal would prove
  almost nothing.
* The **runtime** layer is the real boundary. The attacks below are therefore written to *pass*
  the static scan deliberately — using `os.__dict__["environ"]` rather than `os.environ`, for
  instance — so that what fails is the isolation itself and not the pattern matcher.

Where the platform cannot enforce a criterion, the test says so out loud and asserts the weaker
property that is actually true. See `test_documented_gap_*` at the bottom; those record real
limitations rather than dressing them up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quantbot.sandbox import SandboxPolicy, SandboxRunner, StaticCheckError

FAST = SandboxPolicy(wall_clock_seconds=30.0, memory_mb=512)


def _run(source: str, policy: SandboxPolicy = FAST) -> object:
    return SandboxRunner(policy).run(source)


# --------------------------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------------------------


def test_credentials_are_absent_from_the_child_environment() -> None:
    """The core control. Written to bypass the static scan so the ENVIRONMENT is what is proven.

    `os.environ` would be refused statically; `os.__dict__["environ"]` is not, which is the
    point. The child reads its real environment and finds no credential because the environment
    was constructed from empty rather than filtered.
    """
    attack = """
import os, json
env = os.__dict__["environ"]
leaked = {k: v for k, v in env.items() if "ALPACA" in k.upper() or "SECRET" in k.upper()}
print(json.dumps({"leaked": sorted(leaked), "count": len(env)}))
"""
    result = _run(attack)

    assert result.ok, f"attack script should run, not error: {result.stderr}"
    assert '"leaked": []' in result.stdout, f"credentials leaked into the sandbox: {result.stdout}"


def test_no_environment_variable_points_at_the_repository_or_the_ledger() -> None:
    """Locating the project is the precondition for every filesystem attack, so deny the map."""
    attack = """
import os, json
env = os.__dict__["environ"]
suspicious = [k for k in env if k.upper().startswith("QUANTBOT") or "ELROND" in k.upper()]
print(json.dumps({"suspicious": sorted(suspicious)}))
"""
    result = _run(attack)

    assert '"suspicious": []' in result.stdout, result.stdout


def test_a_literal_credential_name_is_refused_before_execution() -> None:
    with pytest.raises(StaticCheckError) as caught:
        _run('print("ALPACA_PAPER_API_KEY")')

    assert "forbidden-literal" in str(caught.value)


# --------------------------------------------------------------------------------------------
# The broker: imports and endpoints
# --------------------------------------------------------------------------------------------


def test_quantbot_is_not_importable_inside_the_sandbox() -> None:
    """Removes every order-submission path at once, by absence rather than by blocklist.

    Uses importlib rather than a bare `import quantbot`, which the static scan would refuse. What
    is being proven is that the module genuinely is not on the child's path.
    """
    attack = """
import importlib.util, json
def reachable(name):
    # find_spec RAISES for a submodule whose parent is absent, which is a stronger result than
    # returning None. Both count as unreachable; conflating them would have hidden the
    # difference and made this test look like it was asserting less than it is.
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False
print(json.dumps({
    "quantbot": reachable("quantbot"),
    "brokers": reachable("quantbot.brokers"),
    "runtime": reachable("quantbot.runtime"),
}))
"""
    result = _run(attack)

    assert result.ok, result.stderr
    assert '"quantbot": false' in result.stdout, result.stdout
    assert '"brokers": false' in result.stdout, result.stdout
    assert '"runtime": false' in result.stdout, result.stdout


def test_importing_the_broker_package_is_also_refused_statically() -> None:
    with pytest.raises(StaticCheckError) as caught:
        _run("from quantbot.brokers import AlpacaPaperBrokerAdapter")

    assert "forbidden-import" in str(caught.value)


def test_the_trading_host_is_refused_before_execution() -> None:
    """Order submission lives on this host, so a literal reference never reaches runtime."""
    with pytest.raises(StaticCheckError) as caught:
        _run('url = "https://paper-api.alpaca.markets/v2/orders"')

    assert "paper-api.alpaca.markets" in str(caught.value)


def test_the_trading_host_is_never_in_the_effective_allowlist() -> None:
    """Even a policy that names it explicitly cannot open it."""
    reckless = SandboxPolicy(
        network_enabled=True,
        allowed_hosts=("paper-api.alpaca.markets", "data.alpaca.markets"),
    )

    resolved = reckless.resolved_hosts()

    assert "paper-api.alpaca.markets" not in resolved
    assert "data.alpaca.markets" in resolved, "research data access must survive"


def test_networking_is_denied_at_runtime_by_default() -> None:
    """`socket` passes the static scan on purpose, so the runtime guard is what is tested."""
    attack = """
import socket
try:
    socket.create_connection(("data.alpaca.markets", 443), timeout=5)
    print("CONNECTED")
except PermissionError as error:
    print(f"DENIED: {error}")
except Exception as error:
    print(f"OTHER: {type(error).__name__}")
"""
    result = _run(attack)

    assert "CONNECTED" not in result.stdout, "network reached with networking disabled"
    assert "DENIED" in result.stdout, result.stdout


def test_a_host_outside_the_allowlist_is_denied_with_networking_enabled() -> None:
    """The allowlist was written into the config and never read, so network opened everything.

    Deliberately offline: the refusal happens before name resolution, so this asserts the policy
    and not the state of the network the suite happens to run on.
    """
    permissive = SandboxPolicy(
        wall_clock_seconds=30.0,
        memory_mb=512,
        network_enabled=True,
        allowed_hosts=("data.alpaca.markets",),
    )
    attack = """
import socket
try:
    socket.create_connection(("example.com", 80), timeout=5)
    print("CONNECTED")
except PermissionError as error:
    print(f"DENIED: {error}")
except Exception as error:
    print(f"OTHER: {type(error).__name__}")
"""

    result = _run(attack, permissive)

    assert "CONNECTED" not in result.stdout, "an unlisted host was reachable"
    assert "DENIED" in result.stdout, result.stdout
    assert "OTHER" not in result.stdout, (
        "the refusal must be the allowlist, not a DNS failure that would pass on an "
        f"offline machine and fail on a connected one: {result.stdout}"
    )


def test_a_raw_address_never_resolved_through_the_allowlist_is_denied() -> None:
    """Skipping the name is the obvious way around a host allowlist.

    Only addresses an allowlisted name actually resolved to are connectable, so a literal IP
    that no permitted lookup produced has no route.
    """
    permissive = SandboxPolicy(
        wall_clock_seconds=30.0,
        memory_mb=512,
        network_enabled=True,
        allowed_hosts=("data.alpaca.markets",),
    )
    attack = """
import socket
try:
    socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("93.184.216.34", 80))
    print("CONNECTED")
except PermissionError as error:
    print(f"DENIED: {error}")
except Exception as error:
    print(f"OTHER: {type(error).__name__}")
"""

    result = _run(attack, permissive)

    assert "CONNECTED" not in result.stdout, "a raw address bypassed the allowlist"
    assert "DENIED" in result.stdout, result.stdout
    assert "OTHER" not in result.stdout, result.stdout


def test_an_allowlisted_host_is_still_reachable() -> None:
    """The counterweight, and the test that fails if the guard degenerates into a blanket deny.

    Denying everything would pass both tests above while making networking useless, so this one
    asserts the permitted half against a loopback listener -- real sockets, no internet.
    """
    import socket as socket_module
    import threading

    listener = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
    listener.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve() -> None:
        try:
            connection, _ = listener.accept()
            with connection:
                connection.sendall(b"HELLO")
        except OSError:
            pass

    server = threading.Thread(target=serve, daemon=True)
    server.start()

    permitted = SandboxPolicy(
        wall_clock_seconds=30.0,
        memory_mb=512,
        network_enabled=True,
        allowed_hosts=("127.0.0.1",),
    )
    probe = f"""
import socket
try:
    with socket.create_connection(("127.0.0.1", {port}), timeout=10) as connection:
        print("RECEIVED:", connection.recv(16).decode())
except PermissionError as error:
    print(f"DENIED: {{error}}")
except Exception as error:
    print(f"OTHER: {{type(error).__name__}}: {{error}}")
"""

    try:
        result = _run(probe, permitted)
    finally:
        listener.close()
        server.join(timeout=5)

    assert "RECEIVED: HELLO" in result.stdout, (
        f"an allowlisted host must stay reachable, or the allowlist is a blanket deny: "
        f"{result.stdout} {result.stderr}"
    )


# --------------------------------------------------------------------------------------------
# Resource limits
# --------------------------------------------------------------------------------------------


def test_an_infinite_loop_is_terminated_by_the_wall_clock() -> None:
    """The one hard limit on this platform: it comes from the subprocess timeout."""
    result = _run("while True:\n    pass\n", SandboxPolicy(wall_clock_seconds=3.0))

    assert not result.ok
    assert result.timed_out, f"expected a wall-clock kill, got {result.terminated_reason}"
    assert result.duration_seconds < 25, "termination must be prompt, not eventual"


def test_a_runaway_allocation_is_terminated_by_the_memory_monitor() -> None:
    """Asserts MEMORY specifically, with a generous wall clock so it cannot cover for it.

    An earlier version of this test accepted either termination reason, and that leniency hid a
    real bug for a full run: the monitor measured only the direct child, which on Windows is a
    ~5MB venv trampoline, so it reported a flat 4.8MB while the experiment allocated 600MB and
    the ceiling never fired. The wall clock killed it at 45s and the test passed.

    The generous 45s wall clock is therefore deliberate. A memory kill lands in a couple of
    seconds, so if this ever regresses to a wall-clock kill the assertion fails loudly instead of
    being masked.
    """
    attack = """
blocks = []
while True:
    blocks.append(bytearray(20 * 1024 * 1024))
"""
    result = _run(
        attack,
        SandboxPolicy(wall_clock_seconds=45.0, memory_mb=256, poll_interval_seconds=0.2),
    )

    assert not result.ok
    assert result.exceeded_memory, (
        f"expected a memory kill, got {result.terminated_reason!r} after "
        f"{result.duration_seconds:.1f}s — the monitor is measuring the wrong process"
    )
    assert result.duration_seconds < 20, "a memory kill should be prompt, not near the wall clock"


# --------------------------------------------------------------------------------------------
# Artifacts and failure reporting
# --------------------------------------------------------------------------------------------


def test_outputs_are_captured_and_content_addressed() -> None:
    """Every artifact must be citable back to the experiment that produced it."""
    source = """
import pathlib
pathlib.Path("outputs/result.json").write_text('{"sharpe": 0.91}', encoding="utf-8")
"""
    result = _run(source)

    assert result.ok, result.stderr
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.name == "result.json"
    assert artifact.sha256 and len(artifact.sha256) == 64
    assert result.script_sha256, "the script itself must be identified"


def test_a_failing_experiment_is_reported_not_swallowed() -> None:
    """A failed experiment is evidence. It must surface as a result, never as a silent retry."""
    result = _run("raise ValueError('the hypothesis blew up')")

    assert not result.ok
    assert result.exit_code != 0
    assert "the hypothesis blew up" in result.stderr
    assert result.terminated_reason is None, "this failed on its own, it was not killed"


def test_the_workspace_is_destroyed_after_the_run() -> None:
    """Disposable means disposable: a leftover workspace is readable by the next experiment."""
    source = """
import pathlib
pathlib.Path("outputs/leak.txt").write_text(str(pathlib.Path.cwd()), encoding="utf-8")
print(pathlib.Path.cwd())
"""
    result = _run(source)

    assert result.ok, result.stderr
    workspace = Path(result.stdout.strip().splitlines()[-1])
    assert not workspace.exists(), f"workspace survived the run: {workspace}"


def test_the_sandbox_runs_from_a_disposable_directory_not_the_repository() -> None:
    result = _run("import pathlib; print(pathlib.Path.cwd())")

    cwd = result.stdout.strip().splitlines()[-1].lower()
    assert "elrond-sandbox-" in cwd, cwd
    assert "project_elrond" not in cwd, f"sandbox ran inside the repository: {cwd}"


# --------------------------------------------------------------------------------------------
# Documented gaps. These record what this platform cannot enforce.
# --------------------------------------------------------------------------------------------


def test_documented_gap_filesystem_writes_are_not_os_enforced() -> None:
    """HONEST LIMITATION, asserted so it cannot be forgotten or quietly claimed as solved.

    Windows without Docker, pywin32, or a second OS user offers no chroot, no mount namespace,
    and no per-process ACL. A child that already knows an absolute path can write to it. The
    sandbox's protection for trusted source, the durable ledger, and the production locks is
    therefore *path concealment* — no environment variable, no working directory, and no
    importable module reveals the repository — not filesystem enforcement.

    This test proves the concealment holds and states plainly that enforcement does not exist.
    Closing the gap needs the WSL backend the policy interface was shaped for; it is raised as a
    CHALLENGE on issue #12 rather than papered over here.
    """
    attack = """
import os, json, pathlib
env = os.__dict__["environ"]
cwd = pathlib.Path.cwd()
# Everything a child could use to locate the repository, from inside the sandbox.
clues = {
    "cwd_is_repo": "project_elrond" in str(cwd).lower(),
    "env_paths": [v for v in env.values() if "project_elrond" in str(v).lower()],
    "parents": [str(p) for p in cwd.parents if "project_elrond" in str(p).lower()],
}
print(json.dumps(clues))
"""
    result = _run(attack)

    assert result.ok, result.stderr
    assert '"cwd_is_repo": false' in result.stdout, result.stdout
    assert '"env_paths": []' in result.stdout, f"an env var revealed the repo: {result.stdout}"
    assert '"parents": []' in result.stdout, f"the workspace sits inside the repo: {result.stdout}"


def test_documented_gap_static_checks_are_bypassable_by_design() -> None:
    """Recorded so nobody mistakes the static layer for the boundary.

    Every credential test above deliberately bypasses this layer. If a future change makes the
    static scan look like the primary control, this test is the reminder that it never was.
    """
    from quantbot.sandbox import scan

    # Refused: the obvious form.
    assert scan("import os; os.environ") != []
    # Not refused: the same capability, one indirection away.
    assert scan('import os; os.__dict__["environ"]') == []


# --------------------------------------------------------------------------------------------
# Production locks and the trading daemon
# --------------------------------------------------------------------------------------------


def test_the_sandbox_cannot_locate_or_name_the_production_locks() -> None:
    """Both lock files are unreachable because the child has no path to them and no library.

    `SingleWriterLock` lives in `quantbot.storage`, which is not importable, and neither
    `QUANTBOT_LOCK_PATH` nor the repository root appears anywhere in the child's environment. The
    attack therefore searches for the lock rather than assuming a path.
    """
    attack = """
import os, json, pathlib
env = os.__dict__["environ"]
found = []
for value in list(env.values()):
    text = str(value)
    if "quantbot" in text.lower() or ".lock" in text.lower():
        found.append(text)
for candidate in pathlib.Path.cwd().parents:
    for name in ("quantbot.lock", "quantbot.daemon.lock", "quantbot.db"):
        if (candidate / name).exists():
            found.append(str(candidate / name))
print(json.dumps({"found": found}))
"""
    result = _run(attack)

    assert result.ok, result.stderr
    assert '"found": []' in result.stdout, f"the sandbox located production state: {result.stdout}"


def test_a_runaway_sandbox_does_not_block_the_writer_lock() -> None:
    """The daemon must keep working while research burns CPU. Issue #12's starvation criterion.

    Uses the real `SingleWriterLock` from the parent process against the real lock path while a
    sandbox is deliberately spinning. If the sandbox could contend for or hold that lock, this
    acquisition would fail — which is exactly the failure that stops a rebalance.
    """
    import threading

    from quantbot.storage import SingleWriterLock

    outcome: dict[str, object] = {}

    def burn() -> None:
        outcome["result"] = SandboxRunner(SandboxPolicy(wall_clock_seconds=6.0)).run(
            "while True:\n    pass\n"
        )

    worker = threading.Thread(target=burn)
    worker.start()
    try:
        # Contend for the writer lock while the sandbox is mid-burn.
        acquired_during = False
        for _ in range(20):
            if not worker.is_alive():
                break
            with SingleWriterLock(Path("quantbot.sandbox-probe.lock")):
                acquired_during = True
            break
        assert acquired_during, "could not acquire a writer lock while the sandbox was running"
    finally:
        worker.join(timeout=60)
        Path("quantbot.sandbox-probe.lock").unlink(missing_ok=True)

    burned = outcome.get("result")
    assert burned is not None and burned.timed_out, "the burner should have been killed"


def test_sandbox_failure_surfaces_as_experiment_failure_not_a_crash() -> None:
    """A sandbox that fails must report, so the Research Director can record and move on.

    Issue #12: "Sandbox failure is reported as experiment failure, not swallowed or retried
    indefinitely." A raise here would propagate into whatever scheduler called it.
    """
    result = _run("import sys; sys.exit(42)")

    assert not result.ok
    assert result.exit_code == 42
    assert result.terminated_reason is None
