# Learning recovery and visibility implementation plan

> For agentic workers: follow systematic debugging, test-driven development and verification-before-completion. Work only in the existing checkout; no extra worktrees.

**Goal:** Repair demonstrated learning recovery defects and expose truthful model/attempt evidence while Elrond remains stopped.

**Architecture:** Keep the commissioned fixed V3 fitter, chronological validation, activation authority and paper execution boundaries. Consolidate consideration idempotency in the learner, where eligibility and exclusions are already part of the immutable run identity. Add bounded read-only learning evidence to the existing overview.

**Tech stack:** Existing Python, SQLAlchemy, SQLite, Pydantic, Jinja and pytest; no dependencies or new services.

**Spec/authority:** The user's November 5 learning-repair request, relayed from the voice chat and verified against its user transcript. Continue offline cleanup, retain one backup, leave all seven runtime tasks disabled. Earlier broad deployment authority is not reinstated. Routine implementation is already authorized; no additional approval loop.

## Evidence

- Three actual procedures (October 28, October 30, November 2) correctly failed validation; candidate aggregate MSE exceeded both comparators. The November 4 acknowledgement reused exactly the November 2 inputs. Do not change those verdicts or gates.
- A disposable commissioned V3 regression demonstrates that reconciled outcomes can become eligible without changing their IDs/status, but the scheduler repeats an acknowledged insufficient-input result. Direct consideration of the same recovered inputs passes unchanged validation.
- Existing 32 learning/activation tests passed after correcting an isolated test environment path that pointed at the removed checkout. This environment failure is not evidence of an operational fitter failure.

## Global constraints

- No live orders, runtime startup, service enabling, ledger mutation or competing heavy database scan.
- All tests use disposable stores and synthetic providers. Actual receipt reads are bounded indexed reads from the stopped standalone backup.
- Preserve chronological availability, protected-data guards, permanent attempt accounting, validation thresholds and activation checks.
- Keep fixture mechanical proof separate from actual out-of-sample economics. Gross prediction accuracy does not establish net profitability.

## Review focus

- Eligibility recovery without a new outcome ID must reach the learner.
- Repeated unchanged considerations must not refit, consume another attempt, or grow receipts indefinitely.
- Outcomes/protection must retain their schedules; at most one learning task runs.
- Missing, malformed, mismatched, future or private dashboard evidence must fail unavailable and not leak payloads.
- COLLECT, candidate rejection, accepted fitted model, acknowledgement and actual selection mode must remain distinct.

## Task 1 — remove stale scheduler memoization

Files: `src/quantbot/paper_core/roles.py`, `tests/integration/paper_core/test_learning_recovery.py`.

- [x] Reproduce acknowledged refusal after actual reconciliation of unchanged outcomes (RED).
- [x] Remove the revision-only shortcut in `daily_consideration` and the duplicate outer session/revision memoization in `run_role`.
- [x] Keep the existing single asynchronous learning task and delegate repeated-input handling to `consider_learning`, whose run identity includes exclusions.
- [x] Cover outer scheduling, stable replay/attempt counts and a reproducible same-session recovery; measure unchanged replay overhead.
- [x] Run learning, outcome recovery, role independence and paper-only/activation regressions.

## Task 2 — show recorded core learning state

Files: `src/quantbot/console/overview.py`, `src/quantbot/console/core_learning.py`, `src/quantbot/console/templates/_overview.html`, `tests/unit/console/test_core_learning.py`.

- [x] Write failing read-only tests for baseline + rejected latest attempt, fitted model, unavailable/corrupt evidence, account mismatch and private reasons.
- [x] Add a bounded `read_core_learning(session, config_id, now)` read model using singleton/primary-key/latest-attempt lookups, with document hash/binding checks. No historical table hydration or broker calls.
- [x] Show recorded active model type/identity, last acknowledgement, latest charged attempt/reason, train/validation coverage and metrics; label gross targets and stale evidence honestly.
- [x] Show the latest recorded decision's model and selection kind separately from current active state.
- [x] Run console tests and a template/API smoke test; verify query plans and no writes.

## Task 3 — verify and record

- [x] Independently review the changes, run relevant integrated checks and lint, record actual failures and results.
- [x] Save sanitized actual rejection metrics and fixture validation evidence; document any unresolved economic/data gaps.
- [x] Commit logical changes. Keep runtime stopped and report any proposed runtime step separately.
- [ ] Continue and verify offline cleanup independently, including one verified compressed backup. This remains in progress and is not a source-release acceptance claim.
