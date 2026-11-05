# Learning recovery and visibility — November 5, 2026

Source changes: 03c88b0, b0ff9cc, b336882; reviewed against e170b2d. Runtime remains stopped; no application deployment, task enabling, authority transition, or paper order was performed.

## What changed

Removed two scheduler memoization shortcuts that hid newly eligible recovered outcomes when outcome IDs/status were unchanged. Existing immutable learner identity now owns idempotency. One asynchronous learning task and all fixed validation, attempt-budget and activation gates remain intact. Source: 7 lines added, 51 removed in roles.py (44 fewer).

Added bounded recorded-learning evidence to the existing overview: active COLLECT/fitted model, active model's own learning receipt, latest charged attempt and rejection, chronological sample coverage, prediction error and the latest recorded decision's model/selection. It does not claim present execution permission or profitability. Initialization has no fabricated learning receipt. The independent review's one P2 ambiguous label was corrected and independently cleared.

Total learning change versus e170b2d: production/template 312 added, 53 removed (259 net added); tests 639 added. No dependency, service, database schema or new worktree. The diagnostics addition deliberately adds truthful visibility; this scoped repair is not a code-size reduction overall.

## Actual retained evidence

Three procedures correctly failed unchanged validation. Candidate gross-return prediction MSE was 7.63%, 13.86%, and 14.35% worse than the COLLECT comparator on October 28, October 30, and November 2. Recorded metrics and gate results are in recorded-validation.json. No final candidate artifact was accepted. November 4 acknowledged the same 147 inputs used November 2. The active model remains COLLECT. The reproduced scheduler bug is not established as the cause of these historical rejections.

Gross returns are not net profitability. The retained 147 closed exploration lots had aggregate gross P&L -0.016007592, 74 wins and 73 losses, gross profit factor about 0.966853, and 6 positive/6 negative exit dates. Costs were unavailable; zero known-net trades means exact net performance remains unknown. More trades or accepting failed models would not repair this evidence gap.

## Verification

All commands ran from D:/Elrond/App. Isolated pytest runner command prefix: `.venv/Scripts/python.exe -B .superpowers/sdd/runtime-recovery/run_checks.py NAME CAP_SECONDS` followed by the test paths below. Tests used disposable stores and synthetic providers.

| Run | Test paths | Result |
| --- | --- | --- |
| learning-recovery-final 240 | tests/integration/paper_core/test_learning_recovery.py -rP | 2 passed, 82.37s |
| learning-adjacent-green 300 | tests/integration/paper_core/test_outcome_cutoff_recovery.py tests/fault/paper_core/test_outcomes_independent.py tests/integration/paper_core/test_daily_paper_loop.py | 24 passed, 149.34s |
| learning-ledger-invariants 240 | tests/unit/execution/test_accounting.py tests/integration/paper_core/test_costed_loop.py tests/fault/paper_core/test_fill_collision_scope.py tests/unit/reporting/test_paper_costs.py tests/fault/paper_core/test_learning_gross.py | 67 passed, 88.44s |
| learning-safety-properties 240 | tests/property/test_risk_invariants.py tests/property/test_no_lookahead.py tests/fault/paper_core/test_final_send_authority.py tests/fault/paper_core/test_activation_races.py | 52 passed, 63.73s |
| core-learning-review-green 180 | tests/unit/console/test_core_learning.py tests/unit/console/test_overview.py | 45 passed, 25.29s; included in later console suite |
| learning-console-final 300 | tests/unit/console | 333 passed, 120.61s |

Ruff: `.venv/Scripts/ruff.exe check src tests scripts` passed. Broader `ruff check .` also examines historical docs/probes and reported 214 existing findings; these were not silently rewritten. Changed-source mypy checks and git diff --check passed. One existing Starlette/httpx deprecation warning remains. No new full repository coverage measurement or full repository suite is claimed.

Recovery characterization failed before the fix, then passed using the real fitter and unchanged acceptance gates. Repeated settled considerations took 85.9–96.5ms for the 60-outcome fixture, with one fit, one attempt, stable receipts and one concurrent learning task. Large-corpus runtime cost is unmeasured. Full synthetic daily-loop regression covers chronological fit, activation, model use and later outcomes; it proves mechanics, not an economic edge.

Actual immutable-backup read-model smoke at b336882: 15 SELECTs, 0.031s, unchanged source backup. It correctly reports the retained baseline, rejected attempt and reused input receipt. No dashboard service started and no database writes occurred.

## Deployment boundaries and remaining work

The authoritative host remains LEANNES-LAPTOP. All seven tasks stay disabled while offline ledger cleanup and verification run. The installed core remains paper-core-14352cc-20261015. The installed guardian has not yet received the growth correction: restarting that version could regenerate redundant data.

Offline source staging is separate from installation and activation. A subsequent cutover requires verified cleanup/retained backup, exact executable/environment pins, reviewed successor manifest and commissioned authority transition, scheduler ownership and emergency-stop checks, dashboard verification, then separately authorized controlled PAPER operation. Existing installer scripts are not implicitly approved by staging. No live orders are permitted.

Unresolved: genuine net-return evidence and fee provenance; no demonstrated profitable strategy; large-corpus replay overhead; no running-system validation while stopped; cleanup results remain separate until fully verified. This report does not declare the comprehensive refactor complete.
