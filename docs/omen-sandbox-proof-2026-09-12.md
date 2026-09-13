# Sandbox execution proven on the Omen — 2026-09-12

Measured evidence that generated research code executes and is contained on the host Elrond
actually runs on. Reproduce with:

```bash
python scripts/prove_sandbox_on_host.py --json
```

Everything below is output from that script on `LEANNES-LAPTOP` at `2026-09-12T16:32:01Z`, not a
description of what it should do.

## What was blocked before this

Both autonomous research callers — `mine_for_anomalies` and `codegen` — refuse a backend whose
`os_enforced` is False, and they are right to: under `SandboxRunner` an absolute path still
resolves, so a miner can read the dotenv and the durable ledger. The only backend that satisfied
them was `WslSandbox`.

Measured on this host:

| Capability | Result |
|---|---|
| `wsl_backend_available()` | `false` (no distribution installed) |
| `WindowsSandbox.exe` present | `False` — Windows 11 **Home**, SKU 101 |
| `docker` on PATH | `False` |

So no generated experiment could execute at all. Static validation is not an execution sandbox.

## Host and backend selection

```json
"host": {
  "hostname": "Leannes-Laptop",
  "platform": "Windows-10-10.0.26200-SP0",
  "python": "3.11.16",
  "sandbox_root": "C:\\Users\\leepa\\AppData\\Local\\elrond-sandbox"
},
"selection": {
  "wsl_backend_available": false,
  "windows_backend_available": true,
  "selected": "WindowsSandbox",
  "os_enforced": true,
  "unenforced_runner_still_declares_false": true,
  "container_sid": "S-1-15-2-3494209325-677736389-3212704172-2294965094-904372797-1466136105-1070702895",
  "policy": {
    "wall_clock_seconds": 60.0, "memory_mb": 192, "max_output_bytes": 33554432,
    "network_enabled": false, "resolved_hosts": [],
    "forbidden_hosts": ["paper-api.alpaca.markets", "api.alpaca.markets", "broker-api.alpaca.markets"]
  }
}
```

## The legitimate experiment

Run through `mine_for_anomalies` — the caller that gates on `os_enforced` — rather than through
the sandbox directly, so this demonstrates the orchestration path a generated experiment takes and
not merely that a subprocess starts.

```json
"legitimate_experiment": {
  "path": "quantbot.research.mining.mine_for_anomalies",
  "gate": "refuses any backend whose os_enforced is False",
  "started_utc": "2026-09-12T16:32:01+00:00",
  "finished_utc": "2026-09-12T16:32:01+00:00",
  "evaluated": 64,
  "candidates": 1,
  "candidate_ids": ["MINE-20260912-0"],
  "search_cardinality_charged": 64,
  "workspaces_left_behind": [],
  "cleanup": "clean"
}
```

**This is a capability proof, not research evidence.** The miner searches 64 lags of a
deterministic synthetic series seeded inside the script. It reads no market data, consumes no
holdout, touches no reserved confirmatory window, and its candidate is returned in memory and
discarded. Nothing was written to the research registry. `MINE-20260912-0` must never be cited as
a finding; it exists to show that the search cardinality a real miner reports is charged
correctly — 64 evaluated, 64 stamped onto the candidate.

## The experiments that had to fail

### Reaching outside the workspace by absolute path

```json
{"repository-pyproject": "DENIED:PermissionError",
 "deployed-runtime":    "DENIED:PermissionError",
 "user-profile-ntuser": "DENIED:PermissionError",
 "list-profile":        "DENIED:PermissionError",
 "workspace-write":     "OK"}
```

`workspace-write: OK` is the control. Without it, a child that was simply broken would produce the
same four denials and prove nothing. The denials cover the repository checkout, the deployed
runtime checkout, the user profile registry hive, and listing the profile directory at all.

### Allocating past the memory ceiling

```json
{"ok": false, "exit_code": 1, "terminated_reason": "memory",
 "duration_seconds": 0.109, "peak_memory_mb": 127.29, "memory_ceiling_mb": 128,
 "wall_clock_seconds": 90.0,
 "stderr_tail": "...blocks.append(bytearray(8 * 1024 * 1024))\n...\nMemoryError"}
```

The wall clock was 90 seconds and the run ended in 0.109 seconds, so the clock cannot be what
stopped it. Peak 127.29 MB against a 128 MB ceiling, and the child raised `MemoryError` at the
allocation — the Job Object refused the request rather than a monitor noticing afterwards.

### Naming the trading host in generated code

```json
{"outcome": "REFUSED BEFORE EXECUTION", "refusal": "static scan",
 "violations": ["line 3: forbidden-literal — paper-api.alpaca.markets",
                "line 3: forbidden-literal — api.alpaca.markets"]}
```

Refused by the outer layer before any process started.

### A terminated miner, through the research path

```json
{"outcome": "REFUSED",
 "refusal": "the mining run was stopped (memory); a search that did not finish has an unknown
             cardinality, and an unknown cardinality cannot be charged"}
```

This is the one that matters most for statistical integrity. The sandbox stopping a runaway is
only half the requirement; the other half is that the research path must not then record a search
whose size nobody knows. It refuses.

## Cleanup

Every run above reports `"workspaces_left_behind": []` and `"cleanup": "clean"`, including the
terminated one and the one refused before execution.

## Test-suite verification

```
commit       17ac5c4ef17773e8e82d536adeda261119d5a2b3  (claude/windows-sandbox-backend)
command      .venv/Scripts/python.exe -m pytest
started      2026-09-12T16:47:59Z
finished     2026-09-12T16:57:22Z
duration     553.03s
result       2851 passed, 46 skipped, 0 failed, 1 warning
exit code    0
```

No pipeline between pytest and the recorded exit code, so a non-zero status could not have been
swallowed.

### The interpreter the venv is built on is load-bearing

A first attempt reported three failures — `test_service_wrapper_terminates_its_python_process_tree`,
`test_windows_installer_registers_bounded_tasks_through_scheduler_doubles`, and
`test_killed_writer_rolls_back_bars_and_restart_replays_without_loss`. None was caused by this
work, and the cause is worth recording because it will catch the next person.

`deploy/omen/setup-windows.ps1` refuses with `VENV_BASE_NOT_HOST_VISIBLE` when the virtualenv's
*base* interpreter lives under `%APPDATA%` or `%LOCALAPPDATA%\Packages`. A venv created by `uv`
with its default managed Python lands at
`C:\Users\leepa\AppData\Roaming\uv\python\cpython-3.11.16-...`, which is exactly that, so the
installer correctly refuses and the tests that exercise it fail.

The deployed runtime's venv is built on `D:\Elrond\tools\python\cpython-3.11.16-...` instead —
deliberately outside the profile, which is why the same suite reports clean there. Rebuilding the
worktree venv on that same base interpreter makes all three pass:

```
uv venv --python D:\Elrond\tools\python\cpython-3.11.16-windows-x86_64-none\python.exe
uv sync --frozen
-> tests/unit/operations/test_service.py + tests/integration/test_ingestion_recovery.py
   37 passed
```

So the figure above is comparable to the deployed suite rather than an artifact of a worktree.

## What this does not prove

* Nothing here is a market finding, and no research capacity was consumed.
* The container is `denied`, not `absent` — generated code can tell it is sandboxed. See
  `docs/sandbox-contract.md` for the full list of differences from the WSL backend.
* The child's environment still contains the host username, because the workspace lives under the
  user profile. Host shape, not host contents.
