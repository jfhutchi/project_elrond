# Position protection arithmetic and input integrity

**Goal:** Evaluate long and short losses correctly and refuse unusable protection inputs.

**Architecture:** Reuse the existing independent supervisor, durable actions, execution gateway,
and console. Correct the pure evaluator and version its changed semantics as
`position-protection-v2`. Existing historical rows retain v1. No new service or migration.

**Decision:** Issue comments predate merged runtime wiring. Rebuilding that loop would duplicate
working authority. The current evaluator uses unsigned price return for both long and short
positions and can try to construct a zero-quantity exit. Its plain dataclass accepts invalid
thresholds, and its freshness calculation truncates elapsed days and accepts future timestamps.
These defects are independently reproducible without deployment access.

**Tech stack:** Python, Decimal, pytest, existing SQLite fixtures.

- [x] Inspect #157 and all comments, current evaluator, supervisor and execution wiring.
- [x] Verify the existing protection test baseline.
- [x] Add regression tests and observe the named failures: short losses/profits and exact stop
  boundary; zero quantity; invalid prices/drawdown/policy; future and just-expired timestamps.
  Exercise short BUY exits and zero-quantity refusal through the real supervisor/coordinator.
- [x] Correct the evaluator using position-relative return:
  `move = (price - entry) / entry * Decimal(10000)` followed by `move = -move` for a short.
  Refuse nonfinite/nonpositive thresholds at policy construction; reject invalid evaluation
  inputs with typed non-protected outcomes before arithmetic. Compare elapsed timedeltas exactly.
- [x] Bump `PROTECTION_POLICY_VERSION` to `position-protection-v2`; document signed move semantics,
  exact freshness, conservative invalid-input handling, and historical attribution.
- [x] Run focused tests, Ruff, both mypy targets and the isolated full suite. Obtain independent
  review, fix findings, and record measured results.
- [x] Commit and publish a draft PR referencing #157, leaving deployment acceptance open.

Execution uses test-driven changes inline. Validation commands are `pytest -q
tests/unit/risk/test_protection.py tests/unit/operations/test_position_protection_supervisor.py
tests/unit/operations/test_protection_runtime_wiring.py tests/unit/console/test_protection_panel.py`,
`ruff check src tests`, `mypy --platform linux src`, `mypy --platform win32 src`, and `pytest -q`.
The full run uses a temporary HOME and excludes operational environment variables.

Verification: baseline 56 passed; 29 new regression cases failed before the fix; focused 85 passed.
Ruff and linux/win32 mypy passed (225 files each). Full isolated suite: 2,706 passed, 6 skipped,
1 existing Starlette warning in 365.01s. Skips are four live-model and two Windows symlink tests.
Independent review found no actionable issues. No deployment was activated.


Delivered as draft PR https://github.com/jfhutchi/project_elrond/pull/226.
Tested code commit: fd9ade4. The delivery follow-up changes documentation only.

