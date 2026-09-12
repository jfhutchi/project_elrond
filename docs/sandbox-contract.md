# The sandbox contract

What a research sandbox backend must provide, which of its guarantees are security boundaries, and
what each backend actually enforces on the host it runs on.

This document exists because the answer to "can Elrond execute a generated experiment here?" was
being inferred from class names. It is now written down, measured, and testable.

## Why there is a contract at all

Two callers execute code nobody reviewed:

* `quantbot.research.mining.mine_for_anomalies` — a miner reads data by definition.
* `quantbot.research.codegen` — model-authored feature and strategy code.

Both take an `IsolatedSandbox` (`research/mining.py`) and both **refuse a backend whose
`os_enforced` is False**. That refusal is the whole contract in one line, and its stated reason is
specific:

> anomaly mining requires an OS-enforced sandbox; under the Windows backend a miner given an
> absolute path can still read the dotenv and the durable ledger

So `os_enforced = True` is not a label a backend may assert because it feels careful. It is a claim
that **an absolute path handed to generated code does not resolve**.

## The interface

```python
class IsolatedSandbox(Protocol):
    @property
    def os_enforced(self) -> bool: ...
    def run(self, source: str) -> SandboxResult: ...
```

`os_enforced` is a class attribute, not a constructor argument, in every backend. A backend cannot
become OS-enforced by configuration, and a caller that had to infer the answer from a class name
would eventually infer wrong.

`run` takes source and returns a `SandboxResult`. It raises only when the *sandbox* fails. A script
that fails on its own terms returns `ok=False`, because a failed experiment is evidence and must be
recorded rather than discarded.

## The fifteen questions, answered

| # | Requirement | Contract |
|---|---|---|
| 1 | What is guaranteed | Generated code cannot reach the broker, credentials, ledger, or network |
| 2 | Boundary vs convenience | Filesystem containment and network denial are boundaries. Static scan and `sys.path` shaping are convenience |
| 3 | How code enters | `run(source)`; a `PREAMBLE` may precede it; the script is written into the workspace |
| 4 | Filesystem | Read: workspace inputs plus interpreter. Write: workspace outputs only. Everything else denied |
| 5 | Process execution | One child interpreter, isolated mode (`-I -S`), no site-packages, no `PYTHON*` variables |
| 6 | Network | Denied. `network_enabled=True` is **refused at construction** by every OS-enforced backend |
| 7 | Timeouts | `wall_clock_seconds`, hard, enforced by the parent |
| 8 | CPU and memory | `memory_mb`; hard where the platform allows, polled where it does not, stated per backend |
| 9 | stdout and stderr | Captured in full, decoded `utf-8`/`replace`, returned on the result |
| 10 | Environment | Constructed from empty. Nothing inherited. There is no `ALPACA_*` to read |
| 11 | Artifacts | Files under `outputs/` are hashed and returned as `Artifact(name, size_bytes, sha256)` |
| 12 | Cleanup | The workspace is destroyed in a `finally`, always, including on sandbox failure |
| 13 | Failure semantics | Sandbox failure raises. Script failure returns `ok=False`. Termination sets `terminated_reason` |
| 14 | Determinism | `script_sha256` content-addresses what actually executed |
| 15 | Selection | The caller chooses; `os_enforced` is the gate. See "Selection" below |

## What each backend actually enforces

Measured on `LEANNES-LAPTOP` (OMEN by HP 16-n0xxx, Windows 11 Home build 26200, 32 GB) on
2026-09-12. "Measured" means a probe ran and the result is reproduced by a test in this repository;
it does not mean the property was assumed from documentation.

| Property | `SandboxRunner` | `WslSandbox` | `WindowsSandbox` |
|---|---|---|---|
| `os_enforced` | False | True | True |
| Host file read via absolute path | **resolves** | absent (mount ns) | **denied** (AppContainer ACL) |
| Network | monkeypatched `socket` | absent (net ns) | **denied** (no capability, WFP) |
| Memory ceiling | polled, then kill | `RLIMIT_AS`, allocation-time | **Job Object, allocation-time** |
| CPU ceiling | none | `RLIMIT_CPU` | Job Object `JOB_OBJECT_LIMIT_JOB_TIME` |
| Process count | uncapped | `ulimit -u` | **Job Object `ActiveProcessLimit`** |
| Tree termination | `taskkill /T`, at next poll | pid namespace | `TerminateJobObject`, atomic |
| Wall clock | hard | hard | hard |
| Available on this host | yes | **no** | **yes** |

`WslSandbox` is unavailable here and that is the mission blocker this document was written to
close: `wsl_backend_available()` returns False (no distribution installed), Windows Sandbox is
absent because this is a Home SKU, and Docker is not installed. Before `WindowsSandbox`, the only
backend that could run was the one both research callers correctly refuse.

## How `WindowsSandbox` earns `os_enforced = True`

Two kernel primitives, neither of which needs elevation, Docker, or WSL.

### AppContainer, for filesystem and network

The child runs in an AppContainer with **no capabilities**. An AppContainer token fails an access
check against any object whose ACL does not name its SID, so the boundary is default-deny and the
grants are the exception rather than the blocklist:

* the per-run workspace is granted `(OI)(CI)(F)`, read and write;
* a shared, read-only interpreter cache is granted `(OI)(CI)(RX)`, execute only;
* nothing else is granted, so the repository, `.env`, the durable store and the user profile are
  unreachable **whatever path the generated code constructs**.

Network denial is a consequence of the same emptiness. Without `internetClient` the Windows
Filtering Platform refuses the socket in the kernel: `getaddrinfo` fails and `connect` returns
`WinError 10013`. There is nothing in-process to monkeypatch around, which is the difference
between this and `SandboxRunner`'s `PREAMBLE`.

### Job Object, for resources

`JOB_OBJECT_LIMIT_PROCESS_MEMORY` makes the memory ceiling **allocation-time**: the child raises
`MemoryError` when it asks, exactly as `RLIMIT_AS` does under WSL, rather than being killed some
poll interval after the fact. `ActiveProcessLimit` caps process count, the limit `policy.py`
recorded as impossible "on this platform without a Job Object". `KILL_ON_JOB_CLOSE` makes tree
termination atomic and unconditional: closing the handle kills every descendant, so a script that
spawns helpers cannot outlive its own run.

## Differences from WSL, stated rather than glossed

These are real and none of them is hidden behind a passing test.

1. **Denied, not absent.** A mount namespace makes the repository *not exist*; AppContainer makes
   it *permission denied*. Generated code can therefore distinguish "sandboxed" from "file missing"
   on Windows. That is an information leak about the host's shape, not a route to its contents, and
   the contract is about contents.

   The same applies to the host username. The workspace lives under `%LOCALAPPDATA%`, so the
   child's own working directory — and therefore `TEMP`, `TMP` and `LOCALAPPDATA` — contain it. A
   child has to know where it is running, and moving the sandbox root outside the profile would
   put a permanent footprint somewhere this project does not own in exchange for a name the
   contract never promised to keep. Recorded rather than fixed, deliberately.
2. **RSS, not address space.** The Job Object ceiling bounds committed memory; `RLIMIT_AS` bounds
   address space. A process that maps a large file without touching it is bounded by one and not
   the other. Neither is strictly stronger.
3. **No allowlisted egress.** `internetClient` grants *all* internet, not an allowlist, so
   `network_enabled=True` is **refused at construction** rather than honoured by opening everything
   while the policy text still describes a restriction. This matches `WslSandbox`, which refuses
   for the same structural reason. A policy field that is written and never enforced is worse than
   an absent one, because it reads as a control in review.
4. **The interpreter is shared.** The read-only interpreter cache is common to all runs. It is
   granted execute-only and is verified against the base interpreter before use, but two concurrent
   experiments do see the same interpreter files.

Under Elrond's research threat model — generated research code that may be wrong, may be
adversarial about reaching data it should not have, and is never trusted with credentials — the
differences above do not reach the contents the contract protects. They are recorded so that a
later reader can re-decide rather than rediscover.

## Fail closed

`WindowsSandbox` refuses to run rather than running with less isolation than it claims:

* `network_enabled=True` refused at construction;
* AppContainer profile cannot be created or its SID derived, refused;
* the workspace ACL grant fails, refused, and the workspace is destroyed;
* the interpreter cache cannot be staged or verified, refused;
* `CreateProcessW` with the security-capabilities attribute fails, refused.

There is no path on which a failure to establish isolation results in execution. In particular
there is **no fallback to `SandboxRunner`** and no fallback to unrestricted host execution: a
caller that asked for an OS-enforced backend and cannot have one receives an exception, never a
weaker sandbox wearing the stronger name.

## Selection

`quantbot.sandbox.select_isolated_backend()` returns the OS-enforced backend available on this
host, or raises `NoIsolatedBackend`. It never returns `SandboxRunner`, because `SandboxRunner` is
not OS-enforced and the whole purpose of the selector is to answer the question the research
callers gate on.

Order is WSL first, then Windows — not because WSL is better, but because a host with a working
distribution has already paid for it and the two are contract-equivalent. `SandboxRunner` remains
available by direct construction for callers that do not need the guarantee and say so.
