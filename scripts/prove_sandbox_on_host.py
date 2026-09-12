"""Prove, on the host it is run on, that generated research code can execute and is contained.

Run this rather than trusting the test suite alone. The suite asserts the boundary; this records
what actually happened on one machine at one moment, in a form that can be pasted into an issue
and checked later: which backend was chosen, what ran, what it was allowed to do, and what the
sandbox did to the runs that misbehaved.

It goes through `mine_for_anomalies`, not through the sandbox directly. That matters. Anomaly
mining is one of the two callers that refuse a backend whose `os_enforced` is False, so a run that
completes here has passed the same gate a generated experiment passes, through the same
abstraction and the same selection path. Proving `subprocess.Popen` works would prove nothing
about whether Elrond can execute research.

**This is a capability proof, not research evidence.** The miner searches a deterministic
synthetic series seeded in this file. It reads no market data, consumes no holdout, and its
candidates are returned in memory and thrown away. Nothing here is written to the research
registry, and none of it should ever be cited as a finding.

    python scripts/prove_sandbox_on_host.py            # human-readable
    python scripts/prove_sandbox_on_host.py --json     # machine-readable
"""

from __future__ import annotations

import argparse
import json
import platform
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quantbot.research.mining import MiningRefused, mine_for_anomalies
from quantbot.research.registry import DataRole, WindowConsumption
from quantbot.sandbox import (
    NoIsolatedBackend,
    SandboxPolicy,
    SandboxRunner,
    select_isolated_backend,
    windows_backend_available,
    wsl_backend_available,
)
from quantbot.sandbox.static import StaticCheckError
from quantbot.sandbox.windows import SANDBOX_ROOT

#: Deliberately modest. A proof run should not be able to hurt the machine the paper coordinator
#: is running on, and a ceiling this low makes the violation case below arrive quickly.
POLICY = SandboxPolicy(wall_clock_seconds=60.0, memory_mb=192)

#: A real search over a series this script defines, so the result is reproducible anywhere and
#: depends on no dataset. `evaluated` is the honest cardinality of what it looked at, which is the
#: figure `mine_for_anomalies` refuses to accept as a guess.
MINER = '''
import json, random

random.seed(20260912)
series = [random.gauss(0.0, 1.0) for _ in range(2048)]

def autocorrelation(lag):
    pairs = [(series[i], series[i + lag]) for i in range(len(series) - lag)]
    mean_x = sum(x for x, _ in pairs) / len(pairs)
    mean_y = sum(y for _, y in pairs) / len(pairs)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in pairs) / len(pairs)
    var_x = sum((x - mean_x) ** 2 for x, _ in pairs) / len(pairs)
    var_y = sum((y - mean_y) ** 2 for _, y in pairs) / len(pairs)
    return covariance / ((var_x * var_y) ** 0.5)

lags = list(range(1, 65))
scores = {lag: autocorrelation(lag) for lag in lags}
strongest = max(scores, key=lambda lag: abs(scores[lag]))
print("searched", len(lags), "lags; strongest at", strongest, round(scores[strongest], 5))

print(json.dumps({
    "evaluated": len(lags),
    "candidates": [{
        "question": (
            "Does a " + str(strongest) + "-observation lag in the synthetic control series "
            "predict its own next observation?"
        ),
        "overlooked": "nothing; this is a seeded control series with no economic content",
        "why_unpriced": "it is not a market, so there is nobody to price it",
        "expected_direction": "none; the null is true by construction",
        "horizon_observations": 2048,
        "universe": ["SYNTHETIC-CONTROL"],
        "features": ["lagged_self"],
        "confounders": ["sampling noise at 64 lags on 2048 draws"],
        "simplest_refutation": (
            "re-seed the generator and confirm the strongest lag moves, which it must"
        ),
    }],
}))
'''

#: Reaches for an absolute path outside the workspace. The kernel refuses it; the script reports
#: the refusal so the evidence carries the error the child actually saw.
PROBE_FILESYSTEM = '''
import json, os, pathlib
findings = {{}}
for label, target in {targets!r}:
    try:
        pathlib.Path(target).read_bytes()
        findings[label] = "READ"
    except OSError as error:
        findings[label] = "DENIED:" + type(error).__name__
try:
    os.listdir({home!r})
    findings["list-profile"] = "LISTED"
except OSError as error:
    findings["list-profile"] = "DENIED:" + type(error).__name__
pathlib.Path("control.txt").write_text("the interpreter is alive")
findings["workspace-write"] = "OK"
print(json.dumps(findings))
'''

#: Allocates past the ceiling. The Job Object refuses the allocation, the run is terminated, and
#: `mine_for_anomalies` must then refuse to charge a search whose cardinality nobody knows.
VIOLATION_MEMORY = '''
import json
blocks = []
for index in range(400):
    blocks.append(bytearray(8 * 1024 * 1024))
print(json.dumps({"evaluated": 1, "candidates": []}))
'''

#: Names the trading host. Refused by the static scan before anything executes, which is the outer
#: layer of the same boundary.
VIOLATION_BROKER = '''
import json
endpoint = "https://paper-api.alpaca.markets/v2/orders"
print(json.dumps({"evaluated": 1, "candidates": [], "endpoint": endpoint}))
'''


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _host() -> dict[str, Any]:
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "sandbox_root": str(SANDBOX_ROOT),
        "repository": str(Path(__file__).resolve().parents[1]),
    }


def _selection() -> dict[str, Any]:
    backend = select_isolated_backend(POLICY)
    return {
        "wsl_backend_available": wsl_backend_available(),
        "windows_backend_available": windows_backend_available(),
        "selected": type(backend).__name__,
        "os_enforced": backend.os_enforced,
        "unenforced_runner_still_declares_false": SandboxRunner.os_enforced is False,
        "container_sid": getattr(backend, "container_sid", None),
        "policy": {
            "wall_clock_seconds": POLICY.wall_clock_seconds,
            "memory_mb": POLICY.memory_mb,
            "max_output_bytes": POLICY.max_output_bytes,
            "network_enabled": POLICY.network_enabled,
            "resolved_hosts": list(POLICY.resolved_hosts()),
            "forbidden_hosts": list(POLICY.forbidden_hosts()),
        },
    }


def _workspaces() -> set[Path]:
    runs = SANDBOX_ROOT / "runs"
    return set(runs.iterdir()) if runs.is_dir() else set()


def _mining_run() -> dict[str, Any]:
    """The legitimate experiment, through the caller that gates on `os_enforced`."""
    backend = select_isolated_backend(POLICY)
    before = _workspaces()
    started = _now()
    run = mine_for_anomalies(
        backend,
        MINER,
        dataset="SYNTHETIC-CONTROL-2048",
        roles=frozenset({DataRole.DISCOVERY}),
        consumption=WindowConsumption(
            dataset="SYNTHETIC-CONTROL-2048", status="UNTOUCHED", trials=0, consumers=()
        ),
        family_id="sandbox-capability-proof",
        proposed_by="prove_sandbox_on_host.py",
        now=datetime.now(UTC),
    )
    finished = _now()
    leaked = sorted(str(path) for path in _workspaces() - before)
    return {
        "path": "quantbot.research.mining.mine_for_anomalies",
        "gate": "refuses any backend whose os_enforced is False",
        "started_utc": started,
        "finished_utc": finished,
        "evaluated": run.evaluated,
        "candidates": len(run.candidates),
        "candidate_ids": [candidate.candidate_id for candidate in run.candidates],
        "search_cardinality_charged": run.candidates[0].search_cardinality,
        "discard_ratio": run.discard_ratio,
        "workspaces_left_behind": leaked,
        "cleanup": "clean" if not leaked else "LEAKED",
    }


def _direct_run(label: str, source: str, policy: SandboxPolicy) -> dict[str, Any]:
    """One run straight through the backend, recorded field by field."""
    backend = select_isolated_backend(policy)
    before = _workspaces()
    started = _now()
    try:
        result = backend.run(source)
    except StaticCheckError as error:
        return {
            "experiment": label,
            "started_utc": started,
            "finished_utc": _now(),
            "outcome": "REFUSED BEFORE EXECUTION",
            "refusal": "static scan",
            "violations": [str(violation) for violation in getattr(error, "violations", [])]
            or [str(error)],
            "workspaces_left_behind": sorted(str(p) for p in _workspaces() - before),
        }
    leaked = sorted(str(path) for path in _workspaces() - before)
    return {
        "experiment": label,
        "started_utc": started,
        "finished_utc": _now(),
        "ok": result.ok,
        "exit_code": result.exit_code,
        "terminated_reason": result.terminated_reason,
        "duration_seconds": round(result.duration_seconds, 3),
        "peak_memory_mb": result.peak_memory_mb,
        "memory_ceiling_mb": policy.memory_mb,
        "wall_clock_seconds": policy.wall_clock_seconds,
        "script_sha256": result.script_sha256,
        "stdout": result.stdout.strip(),
        "stderr_tail": result.stderr.strip()[-400:],
        "artifacts": [
            {"name": a.name, "size_bytes": a.size_bytes, "sha256": a.sha256}
            for a in result.artifacts
        ],
        "workspaces_left_behind": leaked,
        "cleanup": "clean" if not leaked else "LEAKED",
    }


def _violations() -> list[dict[str, Any]]:
    """The runs that must not be allowed to succeed, and what stopped each one."""
    findings: list[dict[str, Any]] = []

    home = Path.home()
    targets = [
        ("repository-pyproject", str(Path(__file__).resolve().parents[1] / "pyproject.toml")),
        ("deployed-runtime", str(home / "elrond-omen-runtime" / "pyproject.toml")),
        ("user-profile-ntuser", str(home / "NTUSER.DAT")),
    ]
    findings.append(
        _direct_run(
            "reach outside the workspace by absolute path",
            PROBE_FILESYSTEM.format(targets=targets, home=str(home)),
            POLICY,
        )
    )

    findings.append(
        _direct_run(
            "allocate past the memory ceiling",
            VIOLATION_MEMORY,
            SandboxPolicy(wall_clock_seconds=90.0, memory_mb=128),
        )
    )

    findings.append(
        _direct_run("name the trading host in generated code", VIOLATION_BROKER, POLICY)
    )

    # The same memory violation, through the research path, to show what the caller does with a
    # terminated run: it refuses to charge a search whose cardinality nobody can state.
    backend = select_isolated_backend(SandboxPolicy(wall_clock_seconds=90.0, memory_mb=128))
    entry: dict[str, Any] = {
        "experiment": "a terminated miner, through mine_for_anomalies",
        "started_utc": _now(),
    }
    try:
        mine_for_anomalies(
            backend,
            VIOLATION_MEMORY,
            dataset="SYNTHETIC-CONTROL-2048",
            roles=frozenset({DataRole.DISCOVERY}),
            consumption=WindowConsumption(
                dataset="SYNTHETIC-CONTROL-2048", status="UNTOUCHED", trials=0, consumers=()
            ),
            family_id="sandbox-capability-proof",
            proposed_by="prove_sandbox_on_host.py",
            now=datetime.now(UTC),
        )
        entry["outcome"] = "ACCEPTED -- THIS IS A DEFECT"
    except MiningRefused as error:
        entry["outcome"] = "REFUSED"
        entry["refusal"] = str(error)
    entry["finished_utc"] = _now()
    findings.append(entry)

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the evidence as JSON")
    arguments = parser.parse_args()

    evidence: dict[str, Any] = {"recorded_utc": _now(), "host": _host()}
    try:
        evidence["selection"] = _selection()
    except NoIsolatedBackend as error:
        evidence["selection"] = {"error": str(error)}
        print(json.dumps(evidence, indent=2))
        return 1

    evidence["legitimate_experiment"] = _mining_run()
    evidence["violations"] = _violations()

    if arguments.json:
        print(json.dumps(evidence, indent=2))
        return 0

    print(json.dumps(evidence, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
