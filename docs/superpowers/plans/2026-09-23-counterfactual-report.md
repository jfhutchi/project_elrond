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

- [ ] Add temporary-database tests for empty data, policy/version/config separation,
  one candidate with multiple gates, missing/terminal outcomes, and suppressed means.
- [ ] Run `.venv/Scripts/python.exe -m pytest tests/unit/risk/test_counterfactual_report.py -q`;
  expect missing report module/command, then implement the adapter and CLI.
- [ ] Add regime and immutable-history CLI tests; run the same command to green.
- [ ] Run existing counterfactual, gate-value, operations-pass and CLI tests.

## Task 2: Documentation, verification and integration

Files: modify `README.md`; update this execution ledger.

- [ ] Document both command forms, policy selection and observational limitations.
- [ ] Run Ruff and strict mypy, then the full pytest suite. Review failures before claiming success.
- [ ] Commit implementation; obtain independent whole-branch review, fix material findings
  with regression tests, create/attach PR and merge after successful verification.
- [ ] Update #57 with the completed slice and retain broader uncompleted acceptance criteria.

## Execution ledger

Pre-flight: CLI consumes the adapter's JSON-compatible dictionary. No other shared interfaces.
Ruling: use existing isolated worktree and execute inline under the user's approval.
Ruling: missing outcomes do not establish a pending horizon; report their absence explicitly.
Ruling: no dependence cluster is recoverable from the refusal's snapshot identifier alone;
preserve the existing session estimator and disclose the missing cluster context.
