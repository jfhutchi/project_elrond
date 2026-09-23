# Counterfactual Report Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for inline execution.

**Goal:** Deliver the approved read-only operator report in #57.

**Architecture:** A reporting adapter calls existing repository readers and
`report_gate_value`. The CLI dispatches before runtime construction. No schema change.

**Tech Stack:** Python, Pydantic, SQLAlchemy, argparse, pytest.

Spec: `docs/superpowers/specs/2026-09-23-counterfactual-report-design.md`.

## Task 1: Durable adapter and command

Files: create `src/quantbot/risk/counterfactual_report.py` and
`tests/unit/risk/test_counterfactual_report.py`; modify `src/quantbot/cli.py`.

- [x] Add temporary-database tests for empty data, policy/version/config separation,
  one candidate with multiple gates, missing/terminal outcomes, and suppressed means.
- [x] Run `.venv/Scripts/python.exe -m pytest tests/unit/risk/test_counterfactual_report.py -q`;
  expect missing report module/command, then implement the adapter and CLI.
- [x] Add regime and immutable-history CLI tests; run the same command to green.
- [x] Run existing counterfactual, gate-value, operations-pass and CLI tests.

## Task 2: Documentation, verification and integration

Files: modify `README.md`; update this execution ledger.

- [x] Document both command forms, policy selection and observational limitations.
- [x] Run Ruff and strict mypy, then the full pytest suite. Review failures before claiming success.
- [x] Commit implementation; obtain independent whole-branch review, fix material findings
  with regression tests, and create/attach PR #247.

Integration is tracked by PR #247's merge state and issue #57's reporting checklist.
The broader uncompleted acceptance criteria remain open on #57.

## Execution ledger

Pre-flight: CLI consumes the adapter's JSON-compatible dictionary. No other shared interfaces.
Ruling: use existing isolated worktree and execute inline under the user's approval.
Ruling: missing outcomes do not establish a pending horizon; report their absence explicitly.
Ruling: no dependence cluster is recoverable from the refusal's snapshot identifier alone;
preserve the existing session estimator and disclose the missing cluster context.

Task 1: complete. Initial missing-module test failed as expected, then 13 report tests
passed and the focused counterfactual/operations suite passed (87 tests).

Independent branch review identified three material findings: ordinary Database startup
can repair state/change journal mode; duplicate unavailable explanations could be lost;
corrupt files escaped the JSON error response. Regression tests reproduced each failure.
Added `Database.read_only` with SQLite mode=ro, existing metadata validation, no initializer,
and one explicit read transaction. Compared unavailable detail and handled SQLAlchemy
errors specifically. All 21 report tests and 55 report/storage tests passed after fixes.

Review rulings: inherited effective-size and zero-mean verdict semantics stay unchanged;
missing rows do not imply pending horizons; historical policies require explicit selection;
no new snapshot-age threshold is invented; broader uncertainty and economic breakdowns
remain open in #57. These exclusions preserve the bounded adapter scope without claiming
economic validation. No deferred minor review findings.

Initial full suite: 3084 passed, 34 skipped, one existing dependency warning. This run began
before review fixes; a clean committed-snapshot CI run will validate the final implementation.
Ruff and strict mypy passed for all application sources and CI helpers on both platform targets.

Task 2: complete. Final committed-snapshot Windows CI on `1a38798` passed: 3091 tests,
35 skips and one existing Starlette/httpx deprecation warning (524.64 seconds).
Lock/frozen dependency checks, Ruff, Linux/Windows-target mypy and clean-checkout checks
all passed. Native Linux execution was not requested in this run. Final documentation
only records these results; application and test trees are unchanged from the tested SHA.
