# Ingestion Recovery Implementation Plan

> Execute inline using the executing-plans workflow; request independent review before completion.

**Goal:** Make the daily collector's durable history and checkpoint agree with the data it stored.

**Architecture:** Extend the existing MarketDataSync and IngestionControlPlane. Reuse provider
retries, calendar semantics, validation, and immutable bar storage. No schema or deployment change.

**Tech stack:** Python 3.11, asyncio, SQLAlchemy, SQLite, pytest.

- [x] Inspect #198 and comments, current collector, control plane, cache and provider retries.
- [x] Verify baseline: ingestion-control and runtime-wiring tests (29 passed).
- [x] Add regression tests in tests/integration/test_ingestion_recovery.py and observe failures.
- [x] Capture pre-fetch cursor snapshots, record query ranges/modes, bound collection duration,
      extend recovery windows from durable state, and refuse incomplete successful checkpoints.
- [x] Verify replay, duplicate handling, disabled sources, timeouts, retries and concurrency.
- [x] Update docs/ingestion-control.md with behavior, default deadline and operational limitations.
- [x] Run Ruff, both mypy platform targets, and the full isolated test suite; review and fix findings.
- [x] Commit the change and create a draft PR linked to #198 with measured verification results.

## Verification and delivery

Code commit: `38151628509f7c01547c41b34e517df84c5855f4`.
Draft PR: https://github.com/jfhutchi/project_elrond/pull/225.

- Focused ingestion-control, runtime-wiring, and recovery tests: 46 passed, including 17 new
  recovery cases. Regressions were observed failing before their corresponding fixes.
- Independent review identified two additional gaps: an unfinished range hidden by a later
  calendar failure, and a cursor reset/recreation at the same version during an in-flight fetch.
  Both received failing regression tests and fixes; follow-up review found no actionable issues.
- Ruff passed. Mypy passed for linux and win32, 225 source files each. Git diff whitespace checks
  passed.
- Final isolated full suite: 2,694 passed, 6 skipped in 339.08 seconds. WSL integration passed.
  Four skips require live model endpoints; two require Windows symlink privileges. One existing
  Starlette/httpx deprecation warning remains.
- An earlier full run at the same code commit reported WinError 10053 in the unchanged HTTP
  control-server 404 test, with 2,693 other tests passing. The seven control-server tests then
  passed in five consecutive runs, followed by the clean full run. The intermittent socket
  failure is documented, not claimed permanently repaired.

Issue #198 remains open for deliberate deployment and independent scheduling/monitoring.
All code verification used temporary stores; no authoritative deployment database was opened.
